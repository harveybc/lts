# REFERENCE_plugins.md
**LTS (Live Trading System) - Detailed Plugin Design Document**

Este documento describe de forma extensa, precisa y estructurada los **tipos de plugins requeridos en el sistema LTS**, especificando para cada tipo de plugin su funcionalidad exacta, los métodos clave y los parámetros que debe manejar. Todas las clases de plugins deben inicializarse con un parámetro obligatorio `config` (tipo `dict`), el cual se obtiene desde el módulo central `app/config.py` y es sobreescrito por parámetros de CLI, archivos JSON locales o configuraciones remotas.

---

## 1. pipeline plugins

### 1.1 Objetivo
Definen el flujo de procesamiento dentro del ciclo de trading para cada instrumento, integrando decisiones sobre cuándo y cómo actuar en función de las predicciones recibidas del prediction_provider.

### 1.2 Métodos mínimos
- `__init__(self, config: dict)`: inicializa el plugin con la configuración global.
- `run(self, predictions: np.ndarray, market_data: pd.DataFrame) -> dict`: procesa predicciones y datos de mercado para generar señales o decisiones.

### 1.3 Parámetros relevantes en config
- `pipeline.trailing_stop`: configuración de trailing stop en pips o porcentaje.
- `pipeline.take_profit`: configuración de take profit en pips o porcentaje.
- `pipeline.stop_loss`: configuración de stop loss en pips o porcentaje.
- `pipeline.signal_filter`: método de filtrado de señales (e.g., `ema`, `rsi_threshold`).

---

## 2. strategy plugins

### 2.1 Objetivo
Implementan la lógica de toma de decisiones de trading específica para cada instrumento, en base a las señales generadas por el pipeline.

### 2.2 Métodos mínimos
- `__init__(self, config: dict)`: inicializa el plugin con la configuración global.
- `decide(self, signal_data: dict) -> dict`: recibe la información procesada del pipeline y devuelve decisiones como abrir, cerrar o modificar órdenes, junto con los parámetros de la orden (e.g., dirección, volumen).

### 2.3 Parámetros relevantes en config
- `strategy.entry_rule`: definición de la condición para abrir una posición (e.g., cruce de medias, nivel de predicción).
- `strategy.exit_rule`: definición de la condición para cerrar una posición.
- `strategy.max_open_positions`: límite de posiciones abiertas simultáneamente.
- `strategy.position_size`: tamaño estándar de cada operación, si no es determinado por el portfolio_manager.

---

## 3. broker_api plugins

### 3.1 Objetivo
Manejan la conexión y ejecución de operaciones en el broker correspondiente a cada instrumento, soportando brokers reales o simulados.

### 3.2 Métodos mínimos
- `__init__(self, config: dict)`: inicializa el plugin con la configuración global.
- `open_order(self, order_params: dict) -> dict`: abre una orden en el broker con los parámetros especificados.
- `modify_order(self, order_id: str, new_params: dict) -> dict`: modifica una orden existente.
- `close_order(self, order_id: str) -> dict`: cierra una orden abierta.
- `get_open_orders(self) -> list`: devuelve las órdenes abiertas actuales.

### 3.3 Parámetros relevantes en config
- `broker_api.broker_name`: identificador del broker (e.g., `oanda`, `binance`).
- `broker_api.api_key`: clave de acceso al broker.
- `broker_api.api_secret`: secreto de autenticación.
- `broker_api.account_id`: número de cuenta.
- `broker_api.endpoint`: URL base del API REST del broker.

---

## 4. portfolio_manager plugins

### 4.1 Objetivo
Administran la asignación de capital de forma centralizada entre todos los instrumentos activos, aplicando reglas y metodologías de teoría de portafolios (moderna, postmoderna, personalizada).

### 4.2 Métodos mínimos
- `__init__(self, config: dict)`: inicializa el plugin con la configuración global.
- `allocate(self, instruments: list) -> None`: calcula y asigna capital para cada instrumento, modificando su atributo `capital_asignado`.
- `update(self, instrument: object, operation_result: dict) -> None`: recibe resultados de operaciones ejecutadas para ajustar las asignaciones futuras.

### 4.3 Parámetros relevantes en config
- `portfolio_manager.total_capital`: capital total disponible para distribuir.
- `portfolio_manager.allocation_method`: método para asignar capital (e.g., `equal_weight`, `variance_minimization`).
- `portfolio_manager.max_total_exposure`: exposición máxima agregada permitida como porcentaje del capital total.
- `portfolio_manager.rebalance_frequency`: frecuencia de rebalanceo (e.g., `hourly`, `daily`).

---

## 5. instrument configuration

### 5.1 Objetivo
Cada instrumento es tratado como un sistema independiente que combina pipeline, strategy y broker_api, pero todos ellos comparten el mismo portfolio_manager.

### 5.2 Clase sugerida
```python
class Instrumento:
    def __init__(self, nombre: str, pipeline, strategy, broker_api, config: dict):
        self.nombre = nombre
        self.pipeline = pipeline
        self.strategy = strategy
        self.broker_api = broker_api
        self.config = config
        self.capital_asignado = 0
        self.estado_ordenes = []
```        
### 5.3 Parámetros relevantes en config (por instrumento)
- instrument.symbol: símbolo o identificador del instrumento (e.g., EUR/USD).
- instrument.broker: nombre del broker a usar para este instrumento.
- instrument.pipeline: nombre del plugin de pipeline a usar.
- instrument.strategy: nombre del plugin de estrategia.
- instrument.broker_api: nombre del plugin de broker_api.
- instrument.max_positions: número máximo de posiciones simultáneas permitidas para este instrumento.

## 6. Consideraciones generales
    Todas las clases de plugins deben inicializarse obligatoriamente con config: dict para garantizar coherencia con el sistema de configuración global de la aplicación.
    El portfolio_manager debe gestionar todas las instancias de instrumentos activas y ser el punto único de decisión sobre asignación de capital.
    Cada ejecución del daemon LTS debe orquestar el ciclo completo: asignación → predicción → pipeline → estrategia → ejecución en broker, en todos los instrumentos configurados.
    La arquitectura debe soportar ejecución paralela de instrumentos para minimizar la latencia en ciclos con decenas de instrumentos simultáneos.
    Cada plugin debe validar en __init__ la configuración requerida, lanzando excepciones claras si faltan parámetros obligatorios.
