//+------------------------------------------------------------------+
//| LTS MT5 model execution bridge                                   |
//| Authenticated Demo execution with mandatory native SL and TP.    |
//+------------------------------------------------------------------+
#property copyright "LTS Development Team"
#property version   "2.00"
#property strict
#property description "Model-controlled authenticated MT5 Demo execution bridge"

input string InpBridgeUrl = "http://192.168.122.1:8766";
input string InpBridgeSecret = "";
input string InpObservedSymbols = "SOLUSD,ETHUSD,BTCUSD,ADAUSD,DOGEUSD,XRPUSD,USDCAD,EURJPY,EURUSD,AUDUSD,GBPJPY,USDJPY,NZDUSD";
input int InpTimerSeconds = 15;
input int InpSnapshotEveryTimers = 4;
input int InpRequestTimeoutMs = 4000;
input bool InpExecutionEnabled = false;
input string InpTradeSymbol = "ETHUSD";
input double InpMaximumVolume = 0.01;
input int InpMaximumDeviationPoints = 20;
input long InpMagic = 26080301;
input int InpClosedBarHistory = 800;

string ADAPTER_VERSION = "lts.mt5.ea.execution.v2";
string EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855";
string account_fingerprint = "";
string server_fingerprint = "";
int timer_counter = 0;
string last_command_id = "";

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

string IsoUtc(const datetime timestamp)
  {
   MqlDateTime value;
   TimeToStruct(timestamp, value);
   return StringFormat(
      "%04d-%02d-%02dT%02d:%02d:%02dZ",
      value.year, value.mon, value.day, value.hour, value.min, value.sec
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
   if(value == "")
     {
      digest_hex = EMPTY_SHA256;
      return true;
     }
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
   if(!Sha256String("", digest) || digest != EMPTY_SHA256)
      return false;
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

string HeaderValue(const string headers, const string name)
  {
   string normalized_headers = headers;
   string normalized_name = name;
   StringToLower(normalized_headers);
   StringToLower(normalized_name);
   string marker = normalized_name + ":";
   int start = StringFind(normalized_headers, marker);
   if(start < 0)
      return "";
   start += StringLen(marker);
   int finish = StringFind(headers, "\r\n", start);
   if(finish < 0)
      finish = StringLen(headers);
   string value = StringSubstr(headers, start, finish - start);
   StringTrimLeft(value);
   StringTrimRight(value);
   return value;
  }

bool SignedGet(const string path, const string query, string &body)
  {
   string body_hash = "";
   if(!Sha256String("", body_hash))
      return false;
   string timestamp = (string)(long)TimeGMT();
   string nonce = RequestNonce();
   string canonical = "GET\n" + path + "\n" + timestamp + "\n"
                      + nonce + "\n" + body_hash;
   string signature = "";
   if(!HmacSha256(InpBridgeSecret, canonical, signature))
      return false;
   string headers =
      "Accept: text/plain\r\n"
      "X-LTS-Timestamp: " + timestamp + "\r\n"
      "X-LTS-Nonce: " + nonce + "\r\n"
      "X-LTS-Signature: " + signature + "\r\n";
   char request_body[];
   ArrayResize(request_body, 0);
   char response[];
   string response_headers = "";
   ResetLastError();
   int status = WebRequest(
      "GET", InpBridgeUrl + path + query, headers, InpRequestTimeoutMs,
      request_body, response, response_headers
   );
   if(status != 200 && status != 204)
     {
      PrintFormat("LTS bridge GET %s failed: HTTP=%d error=%d", path, status, GetLastError());
      return false;
     }
   body = CharArrayToString(response, 0, WHOLE_ARRAY, CP_UTF8);
   string response_hash = "";
   if(!Sha256String(body, response_hash))
      return false;
   string expected = "";
   if(!HmacSha256(InpBridgeSecret, nonce + "\n" + response_hash, expected))
      return false;
   string observed = HeaderValue(response_headers, "X-LTS-Response-Signature");
   if(observed == "" || observed != expected)
     {
      Print("Refusing unsigned or altered MT5 execution command response");
      return false;
     }
   return true;
  }

bool IsSha256(const string value)
  {
   if(StringLen(value) != 64)
      return false;
   for(int index = 0; index < 64; index++)
     {
      ushort code = StringGetCharacter(value, index);
      if(!((code >= '0' && code <= '9') || (code >= 'a' && code <= 'f')))
         return false;
     }
   return true;
  }

ENUM_ORDER_TYPE_FILLING FillingMode(const string symbol)
  {
   long modes = SymbolInfoInteger(symbol, SYMBOL_FILLING_MODE);
   if((modes & SYMBOL_FILLING_IOC) == SYMBOL_FILLING_IOC)
      return ORDER_FILLING_IOC;
   if((modes & SYMBOL_FILLING_FOK) == SYMBOL_FILLING_FOK)
      return ORDER_FILLING_FOK;
   return ORDER_FILLING_RETURN;
  }

bool SelectLtsPosition(const string symbol)
  {
   for(int index = PositionsTotal() - 1; index >= 0; index--)
     {
      ulong ticket = PositionGetTicket(index);
      if(ticket > 0
         && PositionGetString(POSITION_SYMBOL) == symbol
         && PositionGetInteger(POSITION_MAGIC) == InpMagic)
         return PositionSelectByTicket(ticket);
     }
   return false;
  }

bool PostCommandResult(
   const string command_id,
   const bool success,
   const MqlTradeResult &result,
   const string message
)
  {
   string body =
      "{"
      "\"schema\":\"lts.mt5.execution_result.v1\","
      "\"command_id\":" + JsonString(command_id) + ","
      "\"account_fingerprint\":" + JsonString(account_fingerprint) + ","
      "\"success\":" + JsonBool(success) + ","
      "\"result_code\":" + IntegerToString((int)result.retcode) + ","
      "\"order_ticket\":" + JsonString((string)result.order) + ","
      "\"deal_ticket\":" + JsonString((string)result.deal) + ","
      "\"message\":" + JsonString(message) + ","
      "\"observed_at\":" + JsonString(IsoUtcNow()) +
      "}";
   return SignedPost("/v2/commands/result", body);
  }

bool ExecuteOpen(
   const string command_id,
   const string action,
   const string symbol,
   const double volume,
   const double stop_loss,
   const double take_profit,
   MqlTradeResult &result,
   string &message
)
  {
   if(SelectLtsPosition(symbol))
     {
      string comment = PositionGetString(POSITION_COMMENT);
      if(StringFind(comment, StringSubstr(command_id, 0, 20)) >= 0)
        {
         result.retcode = TRADE_RETCODE_DONE;
         message = "idempotent_existing_position";
         return true;
        }
      message = "route_already_has_lts_position";
      return false;
     }
   if(PositionSelect(symbol))
     {
      message = "route_has_foreign_position";
      return false;
     }
   if(symbol != InpTradeSymbol || volume <= 0 || volume > InpMaximumVolume)
     {
      message = "route_or_volume_outside_ea_mandate";
      return false;
     }
   MqlTick tick;
   if(!SymbolSelect(symbol, true) || !SymbolInfoTick(symbol, tick))
     {
      message = "quote_unavailable";
      return false;
     }
   bool is_long = action == "open_long";
   double reference = is_long ? tick.ask : tick.bid;
   if(reference <= 0
      || (is_long && !(stop_loss < reference && reference < take_profit))
      || (!is_long && !(take_profit < reference && reference < stop_loss)))
     {
      message = "sl_tp_not_anchored_to_direct_quote";
      return false;
     }
   double minimum = SymbolInfoInteger(symbol, SYMBOL_TRADE_STOPS_LEVEL)
                    * SymbolInfoDouble(symbol, SYMBOL_POINT);
   if(MathAbs(reference - stop_loss) < minimum
      || MathAbs(take_profit - reference) < minimum)
     {
      message = "sl_tp_inside_broker_stop_level";
      return false;
     }
   MqlTradeRequest request = {};
   MqlTradeCheckResult check = {};
   ZeroMemory(result);
   request.action = TRADE_ACTION_DEAL;
   request.symbol = symbol;
   request.volume = volume;
   request.type = is_long ? ORDER_TYPE_BUY : ORDER_TYPE_SELL;
   request.price = reference;
   request.sl = stop_loss;
   request.tp = take_profit;
   request.deviation = InpMaximumDeviationPoints;
   request.magic = InpMagic;
   request.comment = "lts:" + StringSubstr(command_id, 0, 20);
   request.type_filling = FillingMode(symbol);
   request.type_time = ORDER_TIME_GTC;
   // A successful OrderCheck reports retcode=0 ("Done"); trade-server
   // retcodes such as TRADE_RETCODE_DONE belong to OrderSend's result.
   if(!OrderCheck(request, check))
     {
      result.retcode = check.retcode;
      message = "order_check_refused:" + check.comment;
      return false;
     }
   if(!OrderSend(request, result))
     {
      message = "order_send_failed:" + result.comment;
      return false;
     }
   bool accepted = result.retcode == TRADE_RETCODE_DONE
                   || result.retcode == TRADE_RETCODE_PLACED
                   || result.retcode == TRADE_RETCODE_DONE_PARTIAL;
   message = accepted ? "protected_entry_accepted" : result.comment;
   return accepted;
  }

bool ExecuteClose(
   const string command_id,
   const string symbol,
   MqlTradeResult &result,
   string &message
)
  {
   ZeroMemory(result);
   if(symbol != InpTradeSymbol)
     {
      message = "close_route_outside_ea_mandate";
      return false;
     }
   if(!SelectLtsPosition(symbol))
     {
      result.retcode = TRADE_RETCODE_DONE;
      message = "already_flat";
      return true;
     }
   ulong ticket = (ulong)PositionGetInteger(POSITION_TICKET);
   double volume = PositionGetDouble(POSITION_VOLUME);
   ENUM_POSITION_TYPE position_type =
      (ENUM_POSITION_TYPE)PositionGetInteger(POSITION_TYPE);
   MqlTick tick;
   if(!SymbolInfoTick(symbol, tick))
     {
      message = "close_quote_unavailable";
      return false;
     }
   MqlTradeRequest request = {};
   MqlTradeCheckResult check = {};
   request.action = TRADE_ACTION_DEAL;
   request.position = ticket;
   request.symbol = symbol;
   request.volume = volume;
   request.type = position_type == POSITION_TYPE_BUY
                  ? ORDER_TYPE_SELL : ORDER_TYPE_BUY;
   request.price = request.type == ORDER_TYPE_BUY ? tick.ask : tick.bid;
   request.deviation = InpMaximumDeviationPoints;
   request.magic = InpMagic;
   request.comment = "lts-close:" + StringSubstr(command_id, 0, 14);
   request.type_filling = FillingMode(symbol);
   if(!OrderCheck(request, check))
     {
      result.retcode = check.retcode;
      message = "close_check_refused:" + check.comment;
      return false;
     }
   if(!OrderSend(request, result))
     {
      message = "close_send_failed:" + result.comment;
      return false;
     }
   bool accepted = result.retcode == TRADE_RETCODE_DONE
                   || result.retcode == TRADE_RETCODE_DONE_PARTIAL;
   message = accepted ? "close_accepted" : result.comment;
   return accepted;
  }

void PollExecutionCommand()
  {
   string body = "";
   // Dual-symbol order 2026-08-23: this EA instance declares its own
   // chart symbol so the bridge only ever delivers commands scoped to
   // it. Two charts (ETHUSD, USDCAD) poll the same account without
   // cross-symbol command theft.
   string query = "?account_fingerprint=" + account_fingerprint
                  + "&symbol=" + Symbol();
   if(!SignedGet("/v2/commands/next", query, body) || body == "")
      return;
   string fields[];
   if(StringSplit(body, '|', fields) != 11 || fields[0] != "v1")
     {
      Print("Refusing malformed MT5 execution command");
      return;
     }
   string command_id = fields[1];
   string action = fields[2];
   string symbol = fields[3];
   double volume = StringToDouble(fields[4]);
   double stop_loss = StringToDouble(fields[5]);
   double take_profit = StringToDouble(fields[6]);
   if(command_id == "" || fields[7] == ""
      || !IsSha256(fields[8]) || !IsSha256(fields[9]) || !IsSha256(fields[10]))
     {
      Print("Refusing MT5 command without complete model evidence");
      return;
     }
   MqlTradeResult result = {};
   string message = "unsupported_action";
   bool success = false;
   // Defense in depth: even a mis-delivered command must fail VISIBLY
   // rather than execute on the wrong chart or linger undelivered.
   if(symbol != Symbol())
     {
      message = "wrong_symbol_for_this_chart";
      PostCommandResult(command_id, false, result, message);
      last_command_id = command_id;
      return;
     }
   if(action == "open_long" || action == "open_short")
      success = ExecuteOpen(
         command_id, action, symbol, volume, stop_loss, take_profit,
         result, message
      );
   else if(action == "close" && volume == 0 && stop_loss == 0 && take_profit == 0)
      success = ExecuteClose(command_id, symbol, result, message);
   PostCommandResult(command_id, success, result, message);
   last_command_id = command_id;
   PostSnapshot();
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
      "\"time_open_unix\":" + (string)PositionGetInteger(POSITION_TIME) + ","
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

string BarJson(const string symbol, const MqlRates &bar)
  {
   datetime utc_time = bar.time - (TimeTradeServer() - TimeGMT());
   return "{"
      "\"symbol\":" + JsonString(symbol) + ","
      "\"timeframe\":\"4h\","
      "\"time\":" + JsonString(IsoUtc(utc_time)) + ","
      "\"open\":" + DoubleToString(bar.open, 10) + ","
      "\"high\":" + DoubleToString(bar.high, 10) + ","
      "\"low\":" + DoubleToString(bar.low, 10) + ","
      "\"close\":" + DoubleToString(bar.close, 10) + ","
      "\"volume\":" + DoubleToString((double)bar.tick_volume, 0) +
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

   string bars = "";
   first = true;
   MqlRates closed_bars[];
   int copied = CopyRates(
      InpTradeSymbol, PERIOD_H4, 1, InpClosedBarHistory, closed_bars
   );
   if(copied > 0)
     {
      ArraySetAsSeries(closed_bars, false);
      for(int index = 0; index < copied; index++)
         AppendJsonItem(bars, BarJson(InpTradeSymbol, closed_bars[index]), first);
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
      "\"symbols\":[" + symbols + "],"
      "\"bars\":[" + bars + "]"
      "}";
   return SignedPost("/v1/snapshot", body);
  }

int OnInit()
  {
   if(!InpExecutionEnabled)
     {
      Print("Refusing to start: MT5 Demo execution was not explicitly enabled");
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
   if(!TerminalInfoInteger(TERMINAL_TRADE_ALLOWED)
      || !MQLInfoInteger(MQL_TRADE_ALLOWED))
     {
      Print("Refusing to start: terminal or EA algorithmic trading is disabled");
      return INIT_FAILED;
     }
   if(InpMaximumVolume <= 0 || InpMaximumVolume > 1.0
      || InpTradeSymbol == "")
      return INIT_PARAMETERS_INCORRECT;
   if(InpTimerSeconds < 5 || InpSnapshotEveryTimers < 1
      || InpClosedBarHistory < 800)
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
      "LTS MT5 Demo execution bridge initialized; account fingerprint=%s",
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
   PollExecutionCommand();
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
