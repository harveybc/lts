# REFERENCE_plugins.md
**LTS (Live Trading System) - Detailed Plugin Design Document**

Este documento describe de forma extensa, precisa y estructurada los **tipos de plugins requeridos en el sistema LTS**, especificando para cada tipo de plugin su funcionalidad exacta, los métodos clave y los parámetros que debe manejar. Todas las clases de plugins deben heredar de `BasePlugin` y seguir la estructura exacta definida.

---

## Plugin Base Structure

All plugins must inherit from `BasePlugin` and follow this exact structure:

```python
from app.plugin_base import BasePlugin

class PluginName(BasePlugin):
    plugin_params = {
        # Plugin-specific parameters with default values
    }
    plugin_debug_vars = ["param1", "param2"]  # Parameters to include in debug info
    
    def __init__(self, config=None):
        super().__init__(config)
        # Plugin-specific initialization
    
    # Plugin-specific methods
    def main_method(self, parameters):
        # Main plugin functionality
        pass
```

---

## 1. AAA (Authentication, Authorization, Accounting) Plugins

### 1.1 Objetivo
Proporcionan autenticación, autorización y contabilidad (auditoría) para todo el sistema.

### 1.2 Métodos específicos
- `authenticate(username, password) -> dict`: Autentica usuario y devuelve token
- `authorize(token, action) -> bool`: Verifica si el token tiene autorización para la acción
- `audit_log(user_id, action, details) -> None`: Registra acción en audit log

### 1.3 Parámetros relevantes
- `session_timeout`: Tiempo de vida de sesión en minutos
- `max_login_attempts`: Intentos máximos de login antes de bloqueo
- `token_secret`: Secreto para generar tokens JWT

---

## 2. Core Plugins

### 2.1 Objetivo
Ejecutan el bucle principal del sistema de trading y manejan el servidor API.

### 2.2 Métodos específicos
- `start() -> None`: Inicia el servidor API y el bucle principal
- `stop() -> None`: Detiene el servidor y limpia recursos
- `execute_trading_loop() -> None`: Ejecuta un ciclo completo de trading

### 2.3 Parámetros relevantes
- `global_latency`: Minutos entre ejecuciones del bucle principal
- `api_host`: Host del servidor API
- `api_port`: Puerto del servidor API

---

## 3. Pipeline Plugins

### 3.1 Objetivo
Orquestan el flujo de trading para cada portfolio, coordinando strategy, broker y portfolio plugins.

### 3.2 Métodos específicos
- `execute_portfolio(portfolio, assets) -> None`: Ejecuta el pipeline completo para un portfolio
- `process_asset(asset, strategy_plugin, broker_plugin) -> dict`: Procesa un asset individual

### 3.3 Parámetros relevantes
- `max_parallel_assets`: Máximo número de assets procesados en paralelo
- `error_retry_count`: Número de reintentos en caso de error

---

## 4. Strategy Plugins

### 4.1 Objetivo
Implementan la lógica de decisión de trading basada en predicciones y datos de mercado.

### 4.2 Métodos específicos
- `process(asset, market_data, predictions) -> dict`: Procesa datos y devuelve acción de trading

### 4.3 Retorno del método process
```python
{
    "action": "open" | "close" | "none",
    "parameters": {
        "order_type": "market" | "limit",
        "side": "buy" | "sell",
        "quantity": float,
        "price": float,  # Para órdenes limit
        "stop_loss": float,
        "take_profit": float
    }
}
```

### 4.4 Parámetros relevantes
- `position_size`: Tamaño base de posición
- `max_risk_per_trade`: Riesgo máximo por operación
- `prediction_threshold`: Umbral de predicción para abrir posiciones

---

## 5. Broker Plugins

### 5.1 Objetivo
Manejan la comunicación con brokers para ejecutar órdenes.

### 5.2 Métodos específicos
- `execute(action, parameters) -> dict`: Ejecuta la acción de trading con el broker

### 5.3 Retorno del método execute
```python
{
    "success": bool,
    "broker_order_id": str,
    "broker_response": dict,
    "error_message": str  # Si success=False
}
```

### 5.4 Parámetros relevantes
- `broker_api_url`: URL del API del broker
- `api_key`: Clave de API del broker
- `account_id`: ID de cuenta del broker
- `timeout`: Timeout para requests al broker

---

## 6. Portfolio Plugins

### 6.1 Objetivo
Gestionan la asignación de capital entre assets de un portfolio.

### 6.2 Métodos específicos
- `allocate_capital(portfolio, assets) -> dict`: Asigna capital a cada asset

### 6.3 Retorno del método allocate_capital
```python
{
    "asset_id": float,  # Capital asignado por asset_id
    "asset_id2": float,
    # ...
}
```

### 6.4 Parámetros relevantes
- `allocation_method`: Método de asignación (equal_weight, risk_parity, etc.)
- `max_allocation_per_asset`: Máximo porcentaje por asset
- `rebalance_threshold`: Umbral para rebalanceo

---

## 7. Database Integration

All plugins must use the SQLAlchemy ORM models for database operations:

- **User**: Usuario system
- **Session**: Sesiones de usuario
- **AuditLog**: Registro de auditoría
- **Config**: Configuración del sistema
- **Statistics**: Estadísticas del sistema
- **Portfolio**: Portfolios de usuario
- **Asset**: Assets dentro de portfolios
- **Order**: Órdenes de trading
- **Position**: Posiciones abiertas

---

## 8. Plugin Configuration

Each plugin receives configuration through:
1. `plugin_params`: Valores por defecto
2. `config` parameter in `__init__`: Configuración global merged
3. JSON configuration stored in database (for portfolios/assets)

---

## 9. Error Handling

All plugins must:
- Handle exceptions gracefully
- Log errors to the audit system
- Return structured error responses
- Not crash the main system

---

## 10. Testing

All plugins must have:
- Unit tests for individual methods
- Integration tests with database
- Mock tests for external APIs
- Performance tests for critical paths

---

## 6. Database Plugin and ORM Integration

All plugins and core modules interact with the database exclusively via SQLAlchemy ORM. The database engine is SQLite by default, but can be swapped for any SQLAlchemy-supported backend. The following tables are required:

### User Table
| Column         | Type        | Description                       |
|---------------|------------|-----------------------------------|
| id            | Integer PK | Unique user ID                    |
| username      | String     | Unique username                   |
| email         | String     | Unique email                      |
| password_hash | String     | Hashed password                   |
| role          | String     | User role (admin, user, etc.)     |
| is_active     | Boolean    | Account active flag               |
| created_at    | DateTime   | Account creation timestamp        |

### Session Table
| Column         | Type        | Description                       |
|---------------|------------|-----------------------------------|
| id            | Integer PK | Unique session ID                 |
| user_id       | Integer FK | Linked user                       |
| token         | String     | Session token (JWT or random)     |
| created_at    | DateTime   | Session creation timestamp        |
| expires_at    | DateTime   | Session expiration                |

### AuditLog Table
| Column         | Type        | Description                       |
|---------------|------------|-----------------------------------|
| id            | Integer PK | Unique log entry                  |
| user_id       | Integer FK | Linked user                       |
| action        | String     | Action performed                  |
| timestamp     | DateTime   | When action occurred              |
| details       | String     | Additional context                |

### Config Table
| Column         | Type        | Description                       |
|---------------|------------|-----------------------------------|
| id            | Integer PK | Unique config entry               |
| key           | String     | Config key                        |
| value         | String     | Config value (JSON/text)          |
| updated_at    | DateTime   | Last update timestamp             |

### Statistics Table
| Column         | Type        | Description                       |
|---------------|------------|-----------------------------------|
| id            | Integer PK | Unique stat entry                 |
| key           | String     | Stat key                          |
| value         | Float      | Stat value                        |
| timestamp     | DateTime   | When stat was recorded            |

All plugin types (AAA, core, pipeline, strategy, broker, portfolio) must use the ORM models for all persistent data. Plugins must not access the database directly except via SQLAlchemy ORM.

---

## 7. Consideraciones generales
    Todas las clases de plugins deben inicializarse obligatoriamente con config: dict para garantizar coherencia con el sistema de configuración global de la aplicación.
    El portfolio_manager debe gestionar todas las instancias de instrumentos activas y ser el punto único de decisión sobre asignación de capital.
    Cada ejecución del daemon LTS debe orquestar el ciclo completo: asignación → predicción → pipeline → estrategia → ejecución en broker, en todos los instrumentos configurados.
    La arquitectura debe soportar ejecución paralela de instrumentos para minimizar la latencia en ciclos con decenas de instrumentos simultáneos.
    Cada plugin debe validar en __init__ la configuración requerida, lanzando excepciones claras si faltan parámetros obligatorios.
