# ArbiTor

Bot de consola en Python para observar oportunidades de compra y venta de tokens en **Base** y **Solana**. Incluye modo de simulación (*paper trading*), adaptadores de cotización e intercambio, registro de operaciones en SQLite, controles de riesgo y métricas de rendimiento.

> **Riesgo:** este software es experimental. No garantiza beneficios ni operaciones rentables. Los precios pueden cambiar entre la cotización y la ejecución; también influyen el deslizamiento, la liquidez, las comisiones, el MEV y los fallos de RPC o del proveedor. Empieza en modo paper y no uses fondos que no puedas permitirte perder.

## Qué hace

En cada ciclo, el bot solicita una cotización para comprar el token objetivo con el token de cotización y otra para volver a venderlo. En modo paper registra el resultado simulado. En modo live puede enviar las transacciones a los proveedores configurados, sujeto a límites de operación y a la aprobación de las señales de riesgo.

El proyecto no implementa arbitraje atómico entre dos mercados: la compra y la venta son transacciones separadas. Puede quedar una posición abierta si la segunda transacción falla. Tampoco garantiza que una cotización incluya todos los costes. Por tanto, una señal favorable no equivale a arbitraje sin riesgo.

### Redes y proveedores

- **Base:** cotizaciones/intercambios mediante la API de 0x; requiere un RPC compatible para operaciones live.
- **Solana:** cotizaciones/intercambios mediante Jupiter Swap API v2; requiere una URL RPC y un keypair para operaciones live.
- **Laya (opcional):** servicio externo que puede actuar como veto de riesgo. No genera ni autoriza operaciones por sí solo. Véase [Laya](https://github.com/NandhaKishorM/laya).

Consulta la documentación de [0x Swap API](https://docs.0x.org/) y [Jupiter Swap API](https://developers.jup.ag/docs/swap) para conocer los requisitos vigentes de acceso y API keys.

## Requisitos

- Python 3.10 o posterior.
- Acceso a Internet para las API de cotización e intercambio.
- Para operaciones live: RPC de la red elegida y credenciales de firma correspondientes.
- Una API key de 0x o Jupiter cuando el proveedor la requiera.

## Instalación

Desde el directorio del proyecto:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
cp .env.example .env
```

En Windows PowerShell, activa el entorno con:

```powershell
.venv\Scripts\Activate.ps1
```

Edita `.env` con los parámetros de tu red y estrategia. El archivo `.env` está excluido del control de versiones: **no publiques claves privadas, frases semilla, API keys ni archivos de wallet**.

## Configuración

Las cantidades de tokens se expresan en unidades mínimas (raw units), no en unidades decimales legibles. Por ejemplo, `1000000` representa 1 USDC si el token tiene 6 decimales. Verifica siempre los decimales y las direcciones de contrato de la red elegida.

### Base

La plantilla configura Base mainnet con USDC como activo de cotización y WETH como objetivo. Revisa las direcciones antes de usar otras redes o tokens.

```dotenv
CHAIN=base
QUOTE_TOKEN=0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913
TARGET_TOKEN=0x4200000000000000000000000000000000000006
AMOUNT_IN=1000000
ZEROX_API_KEY=tu_api_key
RPC_URL=https://tu-rpc-de-base
```

Para live, configura además `EVM_PRIVATE_KEY` y todos los límites de riesgo descritos abajo.

### Solana

Usa los mint addresses de los tokens de Solana y una cantidad en unidades mínimas. Por ejemplo, USDC tiene 6 decimales.

```dotenv
CHAIN=solana
QUOTE_TOKEN=<mint-del-token-de-cotizacion>
TARGET_TOKEN=<mint-del-token-objetivo>
AMOUNT_IN=1000000
JUPITER_API_KEY=tu_api_key
RPC_URL=https://tu-rpc-de-solana
```

Para live, configura `SOLANA_KEYPAIR_PATH` con la ruta a un keypair local protegido. No guardes el archivo del keypair en este repositorio.

### Umbrales y límites

Valores relevantes de `.env`:

| Variable | Uso |
| --- | --- |
| `LIVE_TRADING` | Debe ser `true` para permitir ejecución live; el modo por defecto es paper. |
| `AMOUNT_IN` | Cantidad de entrada en unidades mínimas del token de cotización. |
| `MIN_PROFIT_QUOTE` | Beneficio neto mínimo requerido para considerar la operación, en unidades mínimas del token de cotización. |
| `ESTIMATED_COST_QUOTE` | Reserva estimada de costes, en las mismas unidades. Ajusta este valor a la red y al tamaño de operación. |
| `SLIPPAGE_BPS` | Tolerancia al deslizamiento en puntos básicos (100 bps = 1 %). |
| `MAX_TRADE_AMOUNT` | Límite máximo por operación; debe ser mayor que cero para live. |
| `MAX_DAILY_LOSS` | Pérdida diaria máxima que activa el bloqueo; debe ser mayor que cero para live. |
| `MAX_TRADES_PER_HOUR` | Máximo de operaciones por hora. |
| `INTERVAL_SECONDS` | Pausa entre ciclos del daemon. |
| `DATABASE_PATH` | Ubicación de la base SQLite con el historial. |
| `METRICS_WINDOW` | Número de cotizaciones recientes usado por las métricas. |
| `METRICS_MIN_SAMPLES` | Muestras mínimas requeridas antes de habilitar el filtro estadístico. |
| `MIN_WIN_PROBABILITY` | Límite inferior de probabilidad estimada de superar el umbral de beneficio. |
| `LAYA_ENABLED` | Activa la consulta opcional al servicio Laya. |
| `LAYA_BASE_URL` | URL base de Laya; por defecto apunta al servicio local de ejemplo. |
| `LAYA_API_KEY` | Clave opcional si el servidor Laya está protegido. |
| `LAYA_BLOCK_PROBABILITY` | Umbral de riesgo comunicado por Laya a partir del cual se veta la operación. |

El modo live valida que haya límites positivos configurados y que `AMOUNT_IN` no supere `MAX_TRADE_AMOUNT`. No habilites live hasta revisar estos valores, las credenciales, las direcciones y los permisos de la wallet.

## Uso

Desde el directorio del proyecto, con el entorno virtual activado:

```bash
# Un ciclo simulado
python -m bot --once

# Daemon en modo paper (continúa hasta interrumpirlo con Ctrl+C)
python -m bot

# Mostrar métricas almacenadas
python -m bot --metrics
```

La ejecución live requiere **ambas** condiciones: `LIVE_TRADING=true` en `.env` y el argumento explícito `--live`:

```bash
python -m bot --live
```

Comprueba el comportamiento en paper y consulta las métricas antes de habilitar live. El bot guarda el historial en `DATABASE_PATH` (por defecto, `data/trades.sqlite3`). Respaldar o borrar esa base cambia las métricas históricas disponibles.

## Salida esperada

El texto exacto depende de la red, el proveedor y las cotizaciones. En un ciclo paper, el programa informa de la oportunidad y de si pasa los filtros; una salida ilustrativa sería:

```text
2026-10-12 10:00:00 INFO chain=base mode=paper pair=USDC/WETH
2026-10-12 10:00:01 INFO quote buy=... sell=... estimated_pnl_quote=...
2026-10-12 10:00:01 INFO decision=skip reason=profit_below_threshold
```

La orden puede omitirse si no supera los filtros, faltan datos, el servicio de riesgo la veta o se alcanza un límite. En paper no se envía ninguna transacción a la blockchain.

`--metrics` presenta estadísticas de las muestras almacenadas, además del PnL de fills live cuando existen. Los importes de PnL quote se registran en unidades mínimas del token de cotización; conviértelos a unidades legibles usando sus decimales. Las tasas estimadas se calculan a partir de cotizaciones históricas, no son una predicción garantizada del resultado de futuras operaciones. El resultado realizado puede diferir por fallos, costes o cambios de precio.

## Métricas y filtros

El filtro usa una ventana de cotizaciones round-trip recientes para el par y la red actuales. Antes de alcanzar `METRICS_MIN_SAMPLES`, no hay base suficiente para aplicar el criterio estadístico. Una vez alcanzada, calcula una estimación conservadora de la probabilidad de que las muestras superen `MIN_PROFIT_QUOTE` y de su PnL. El sistema puede descartar una operación que no cumpla el umbral.

Estas métricas sirven como filtro y seguimiento, no como inteligencia predictiva validada. No garantizan que las muestras sean representativas ni que una cotización se pueda ejecutar al mismo precio. Laya es opcional y funciona como veto adicional: una respuesta favorable no elimina los demás controles ni demuestra que la operación vaya a ser rentable.

## Operación live y seguridad

- Usa una wallet dedicada con fondos limitados y permisos mínimos.
- Guarda secretos fuera del repositorio y restringe permisos de lectura de `.env` y del keypair.
- Verifica red, contratos/mints, decimales, tamaño y liquidez antes de operar.
- Configura límites diarios y por operación conservadores; observa el daemon y el historial.
- Asegúrate de que la cotización contempla costes suficientes. La estimación de costes en una moneda distinta de la de cotización puede no estar reflejada por completo en el PnL.
- Una compra y su venta posterior no forman una transacción atómica. Si falla la venta, el bot no puede garantizar la salida de la posición.
- El almacenamiento de métricas y operaciones es local en SQLite; la pérdida o modificación de la base afecta al historial mostrado.

## Estructura del proyecto

```text
token_arb_bot/
├── bot/                 # CLI, configuración, estrategia, riesgo y adaptadores
├── data/                # Base de datos local (ignorada por Git)
├── .env.example         # Plantilla de configuración sin secretos
├── requirements.txt     # Dependencias Python
└── README.md
```

## Licencia

El repositorio todavía no incluye un archivo `LICENSE`. Hasta que se añada una licencia, no se concede permiso explícito para redistribuir o reutilizar el código.
