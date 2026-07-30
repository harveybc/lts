//+------------------------------------------------------------------+
//| LTS MT5 read-only bridge                                         |
//| Demo capability, reconciliation and execution-cost observation.  |
//+------------------------------------------------------------------+
#property copyright "LTS Development Team"
#property version   "1.00"
#property strict
#property description "Read-only authenticated bridge from MT5 Demo to LTS"

input string InpBridgeUrl = "http://192.168.122.1:8766";
input string InpBridgeSecret = "";
input string InpObservedSymbols = "EURUSD,GBPJPY,USDCAD,USDJPY,NZDUSD,EURJPY";
input int InpTimerSeconds = 15;
input int InpSnapshotEveryTimers = 4;
input int InpRequestTimeoutMs = 4000;
input bool InpReadOnly = true;

string ADAPTER_VERSION = "lts.mt5.ea.readonly.v1";
string account_fingerprint = "";
string server_fingerprint = "";
int timer_counter = 0;

string JsonEscape(const string value)
  {
   string result = value;
   StringReplace(result, "\\", "\\\\");
   StringReplace(result, "\"", "\\\"");
   StringReplace(result, "\r", "\\r");
   StringReplace(result, "\n", "\\n");
   StringReplace(result, "\t", "\\t");
   return result;
  }

string JsonString(const string value)
  {
   return "\"" + JsonEscape(value) + "\"";
  }

string JsonBool(const bool value)
  {
   return value ? "true" : "false";
  }

string IsoUtcNow()
  {
   MqlDateTime value;
   TimeToStruct(TimeGMT(), value);
   return StringFormat(
      "%04d-%02d-%02dT%02d:%02d:%02dZ",
      value.year,
      value.mon,
      value.day,
      value.hour,
      value.min,
      value.sec
   );
  }

bool Utf8Bytes(const string value, uchar &output[])
  {
   int size = StringToCharArray(value, output, 0, WHOLE_ARRAY, CP_UTF8);
   if(size <= 0)
      return false;
   ArrayResize(output, size - 1);
   return true;
  }

bool Sha256(const uchar &source[], uchar &digest[])
  {
   uchar unused_key[];
   ArrayResize(unused_key, 0);
   ResetLastError();
   int size = CryptEncode(CRYPT_HASH_SHA256, source, unused_key, digest);
   if(size != 32)
     {
      PrintFormat("SHA256 failed: size=%d error=%d", size, GetLastError());
      return false;
     }
   return true;
  }

string HexEncode(const uchar &source[])
  {
   string result = "";
   for(int index = 0; index < ArraySize(source); index++)
      result += StringFormat("%02x", source[index]);
   return result;
  }

bool Sha256String(const string value, string &digest_hex)
  {
   uchar source[];
   uchar digest[];
   if(!Utf8Bytes(value, source) || !Sha256(source, digest))
      return false;
   digest_hex = HexEncode(digest);
   return true;
  }

bool HmacSha256(const string key_text, const string message, string &digest_hex)
  {
   uchar key[];
   uchar message_bytes[];
   if(!Utf8Bytes(key_text, key) || !Utf8Bytes(message, message_bytes))
      return false;

   if(ArraySize(key) > 64)
     {
      uchar reduced_key[];
      if(!Sha256(key, reduced_key))
         return false;
      ArrayCopy(key, reduced_key);
      ArrayResize(key, ArraySize(reduced_key));
     }

   uchar inner_pad[];
   uchar outer_pad[];
   ArrayResize(inner_pad, 64);
   ArrayResize(outer_pad, 64);
   ArrayInitialize(inner_pad, 0x36);
   ArrayInitialize(outer_pad, 0x5c);
   for(int index = 0; index < ArraySize(key); index++)
     {
      inner_pad[index] = (uchar)(key[index] ^ 0x36);
      outer_pad[index] = (uchar)(key[index] ^ 0x5c);
     }

   uchar inner_source[];
   ArrayResize(inner_source, 64 + ArraySize(message_bytes));
   ArrayCopy(inner_source, inner_pad, 0, 0, 64);
   ArrayCopy(inner_source, message_bytes, 64, 0, WHOLE_ARRAY);
   uchar inner_digest[];
   if(!Sha256(inner_source, inner_digest))
      return false;

   uchar outer_source[];
   ArrayResize(outer_source, 64 + ArraySize(inner_digest));
   ArrayCopy(outer_source, outer_pad, 0, 0, 64);
   ArrayCopy(outer_source, inner_digest, 64, 0, WHOLE_ARRAY);
   uchar digest[];
   if(!Sha256(outer_source, digest))
      return false;
   digest_hex = HexEncode(digest);
   return true;
  }

bool CryptoSelfTest()
  {
   string digest = "";
   if(!HmacSha256(
      "key",
      "The quick brown fox jumps over the lazy dog",
      digest
   ))
      return false;
   return digest ==
      "f7bc83f430538424b13298e6aa6fb143ef4d59a14946175997479dbc2d1a3cd8";
  }

string RequestNonce()
  {
   return StringFormat(
      "%I64d-%I64u-%d",
      (long)TimeGMT(),
      GetMicrosecondCount(),
      MathRand()
   );
  }

bool SignedPost(const string path, const string body)
  {
   uchar unsigned_body[];
   if(!Utf8Bytes(body, unsigned_body))
      return false;
   char request_body[];
   ArrayResize(request_body, ArraySize(unsigned_body));
   for(int index = 0; index < ArraySize(unsigned_body); index++)
      request_body[index] = (char)unsigned_body[index];

   string body_hash = "";
   if(!Sha256String(body, body_hash))
      return false;
   string timestamp = (string)(long)TimeGMT();
   string nonce = RequestNonce();
   string canonical = "POST\n" + path + "\n" + timestamp + "\n"
                      + nonce + "\n" + body_hash;
   string signature = "";
   if(!HmacSha256(InpBridgeSecret, canonical, signature))
      return false;

   string headers =
      "Content-Type: application/json\r\n"
      "Accept: application/json\r\n"
      "X-LTS-Timestamp: " + timestamp + "\r\n"
      "X-LTS-Nonce: " + nonce + "\r\n"
      "X-LTS-Signature: " + signature + "\r\n";
   char response[];
   string response_headers = "";
   ResetLastError();
   int status = WebRequest(
      "POST",
      InpBridgeUrl + path,
      headers,
      InpRequestTimeoutMs,
      request_body,
      response,
      response_headers
   );
   if(status < 200 || status >= 300)
     {
      PrintFormat(
         "LTS bridge POST %s failed: HTTP=%d error=%d",
         path,
         status,
         GetLastError()
      );
      return false;
     }
   return true;
  }

bool BuildIdentity()
  {
   string account_source = StringFormat(
      "%I64d|%s",
      AccountInfoInteger(ACCOUNT_LOGIN),
      AccountInfoString(ACCOUNT_SERVER)
   );
   string server_source = AccountInfoString(ACCOUNT_SERVER);
   string account_hash = "";
   string server_hash = "";
   if(!Sha256String(account_source, account_hash)
      || !Sha256String(server_source, server_hash))
      return false;
   account_fingerprint = StringSubstr(account_hash, 0, 24);
   server_fingerprint = StringSubstr(server_hash, 0, 24);
   return true;
  }

bool PostHeartbeat()
  {
   double ping_ms = (double)TerminalInfoInteger(TERMINAL_PING_LAST) / 1000.0;
   string body =
      "{"
      "\"schema\":\"lts.mt5.heartbeat.v1\","
      "\"adapter_version\":" + JsonString(ADAPTER_VERSION) + ","
      "\"account_fingerprint\":" + JsonString(account_fingerprint) + ","
      "\"server_fingerprint\":" + JsonString(server_fingerprint) + ","
      "\"environment\":\"demo\","
      "\"connected\":" + JsonBool(
         (bool)TerminalInfoInteger(TERMINAL_CONNECTED)
      ) + ","
      "\"trade_allowed\":" + JsonBool(
         (bool)TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)
      ) + ","
      "\"terminal_build\":" + IntegerToString(
         TerminalInfoInteger(TERMINAL_BUILD)
      ) + ","
      "\"terminal_ping_ms\":" + DoubleToString(ping_ms, 3) + ","
      "\"observed_at\":" + JsonString(IsoUtcNow()) +
      "}";
   return SignedPost("/v1/heartbeat", body);
  }

string PositionJson(const int index)
  {
   ulong ticket = PositionGetTicket(index);
   if(ticket == 0)
      return "";
   long position_type = PositionGetInteger(POSITION_TYPE);
   string side = position_type == POSITION_TYPE_BUY ? "long" : "short";
   return "{"
      "\"ticket\":" + JsonString((string)ticket) + ","
      "\"symbol\":" + JsonString(PositionGetString(POSITION_SYMBOL)) + ","
      "\"side\":" + JsonString(side) + ","
      "\"volume\":" + DoubleToString(PositionGetDouble(POSITION_VOLUME), 8) + ","
      "\"price_open\":" + DoubleToString(
         PositionGetDouble(POSITION_PRICE_OPEN), 10
      ) + ","
      "\"stop_loss\":" + DoubleToString(PositionGetDouble(POSITION_SL), 10) + ","
      "\"take_profit\":" + DoubleToString(PositionGetDouble(POSITION_TP), 10) + ","
      "\"profit\":" + DoubleToString(PositionGetDouble(POSITION_PROFIT), 8)
      + "}";
  }

string OrderJson(const int index)
  {
   ulong ticket = OrderGetTicket(index);
   if(ticket == 0)
      return "";
   return "{"
      "\"ticket\":" + JsonString((string)ticket) + ","
      "\"symbol\":" + JsonString(OrderGetString(ORDER_SYMBOL)) + ","
      "\"order_type\":" + JsonString(
         EnumToString((ENUM_ORDER_TYPE)OrderGetInteger(ORDER_TYPE))
      ) + ","
      "\"volume\":" + DoubleToString(OrderGetDouble(ORDER_VOLUME_CURRENT), 8) + ","
      "\"price_open\":" + DoubleToString(OrderGetDouble(ORDER_PRICE_OPEN), 10) + ","
      "\"stop_loss\":" + DoubleToString(OrderGetDouble(ORDER_SL), 10) + ","
      "\"take_profit\":" + DoubleToString(OrderGetDouble(ORDER_TP), 10) + ","
      "\"state\":" + JsonString(
         EnumToString((ENUM_ORDER_STATE)OrderGetInteger(ORDER_STATE))
      ) + "}";
  }

string SymbolJson(const string symbol)
  {
   if(!SymbolSelect(symbol, true))
      return "";
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick) || tick.bid <= 0 || tick.ask <= 0)
      return "";
   double point = SymbolInfoDouble(symbol, SYMBOL_POINT);
   double volume_min = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MIN);
   double volume_max = SymbolInfoDouble(symbol, SYMBOL_VOLUME_MAX);
   double volume_step = SymbolInfoDouble(symbol, SYMBOL_VOLUME_STEP);
   if(point <= 0 || volume_min <= 0 || volume_max <= 0 || volume_step <= 0)
      return "";
   return "{"
      "\"symbol\":" + JsonString(symbol) + ","
      "\"bid\":" + DoubleToString(tick.bid, 10) + ","
      "\"ask\":" + DoubleToString(tick.ask, 10) + ","
      "\"point\":" + DoubleToString(point, 10) + ","
      "\"volume_min\":" + DoubleToString(volume_min, 8) + ","
      "\"volume_max\":" + DoubleToString(volume_max, 8) + ","
      "\"volume_step\":" + DoubleToString(volume_step, 8) + ","
      "\"trade_mode\":" + IntegerToString(
         SymbolInfoInteger(symbol, SYMBOL_TRADE_MODE)
      ) + ","
      "\"observed_at\":" + JsonString(IsoUtcNow()) +
      "}";
  }

void AppendJsonItem(string &array_body, const string item, bool &first)
  {
   if(item == "")
      return;
   if(!first)
      array_body += ",";
   array_body += item;
   first = false;
  }

bool PostSnapshot()
  {
   string positions = "";
   bool first = true;
   for(int index = 0; index < PositionsTotal(); index++)
      AppendJsonItem(positions, PositionJson(index), first);

   string orders = "";
   first = true;
   for(int index = 0; index < OrdersTotal(); index++)
      AppendJsonItem(orders, OrderJson(index), first);

   string symbols = "";
   first = true;
   string selected_symbols[];
   int symbol_count = StringSplit(InpObservedSymbols, ',', selected_symbols);
   for(int index = 0; index < symbol_count; index++)
     {
      string symbol = selected_symbols[index];
      StringTrimLeft(symbol);
      StringTrimRight(symbol);
      AppendJsonItem(symbols, SymbolJson(symbol), first);
     }

   string body =
      "{"
      "\"schema\":\"lts.mt5.snapshot.v1\","
      "\"account_fingerprint\":" + JsonString(account_fingerprint) + ","
      "\"observed_at\":" + JsonString(IsoUtcNow()) + ","
      "\"currency\":" + JsonString(AccountInfoString(ACCOUNT_CURRENCY)) + ","
      "\"balance\":" + DoubleToString(AccountInfoDouble(ACCOUNT_BALANCE), 8) + ","
      "\"equity\":" + DoubleToString(AccountInfoDouble(ACCOUNT_EQUITY), 8) + ","
      "\"margin\":" + DoubleToString(AccountInfoDouble(ACCOUNT_MARGIN), 8) + ","
      "\"free_margin\":" + DoubleToString(
         AccountInfoDouble(ACCOUNT_MARGIN_FREE), 8
      ) + ","
      "\"positions\":[" + positions + "],"
      "\"orders\":[" + orders + "],"
      "\"symbols\":[" + symbols + "]"
      "}";
   return SignedPost("/v1/snapshot", body);
  }

int OnInit()
  {
   if(!InpReadOnly)
     {
      Print("Refusing to start: this EA version is read-only");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(StringLen(InpBridgeSecret) < 32)
     {
      Print("Refusing to start: bridge secret must contain at least 32 characters");
      return INIT_PARAMETERS_INCORRECT;
     }
   if(AccountInfoInteger(ACCOUNT_TRADE_MODE) != ACCOUNT_TRADE_MODE_DEMO)
     {
      Print("Refusing to start outside an MT5 demo account");
      return INIT_FAILED;
     }
   if(InpTimerSeconds < 5 || InpSnapshotEveryTimers < 1)
      return INIT_PARAMETERS_INCORRECT;
   if(!CryptoSelfTest())
     {
      Print("Refusing to start: HMAC-SHA256 self-test failed");
      return INIT_FAILED;
     }
   MathSrand((int)GetTickCount());
   if(!BuildIdentity())
      return INIT_FAILED;
   EventSetTimer(InpTimerSeconds);
   PrintFormat(
      "LTS MT5 read-only bridge initialized; account fingerprint=%s",
      account_fingerprint
   );
   return INIT_SUCCEEDED;
  }

void OnDeinit(const int reason)
  {
   EventKillTimer();
  }

void OnTimer()
  {
   timer_counter++;
   PostHeartbeat();
   if(timer_counter == 1 || timer_counter % InpSnapshotEveryTimers == 0)
      PostSnapshot();
  }

void OnTradeTransaction(
   const MqlTradeTransaction &transaction,
   const MqlTradeRequest &request,
   const MqlTradeResult &result
)
  {
   string event_id_source = StringFormat(
      "%I64d|%I64u|%I64u|%d|%I64u",
      (long)TimeGMT(),
      transaction.order,
      transaction.deal,
      transaction.type,
      GetMicrosecondCount()
   );
   string event_hash = "";
   if(!Sha256String(event_id_source, event_hash))
      return;
   string body =
      "{"
      "\"schema\":\"lts.mt5.trade_event.v1\","
      "\"event_id\":" + JsonString(StringSubstr(event_hash, 0, 32)) + ","
      "\"account_fingerprint\":" + JsonString(account_fingerprint) + ","
      "\"event_type\":" + JsonString(EnumToString(transaction.type)) + ","
      "\"order_ticket\":" + JsonString((string)transaction.order) + ","
      "\"deal_ticket\":" + JsonString((string)transaction.deal) + ","
      "\"symbol\":" + JsonString(transaction.symbol) + ","
      "\"volume\":" + DoubleToString(transaction.volume, 8) + ","
      "\"price\":" + DoubleToString(transaction.price, 10) + ","
      "\"result_code\":" + IntegerToString(result.retcode) + ","
      "\"observed_at\":" + JsonString(IsoUtcNow()) +
      "}";
   SignedPost("/v1/events", body);
  }
