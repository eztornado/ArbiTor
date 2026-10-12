# ArbiTor

Escáner de oportunidades de arbitraje de precio entre agregadores y DEX en **Base** y **Solana**, escrito en Python. Consulta cotizaciones en paralelo, compara comprar en una fuente y vender en otra, descuenta una reserva de costes configurable y guarda el historial en SQLite.

> **Riesgo:** este software es experimental. No garantiza beneficios ni operaciones rentables. Los precios pueden cambiar entre la cotización y la ejecución; también influyen el deslizamiento, la liquidez, las comisiones, el MEV y los fallos de RPC o del proveedor. Empieza en modo paper y no uses fondos que no puedas permitirte perder.

## Qué hace

En cada ciclo, solicita cotizaciones de compra y venta a los proveedores configurados y solo considera una oportunidad si las dos patas usan fuentes distintas. En Base compara 0x, Odos y Aerodrome. En Solana compara Raydium, Orca, Meteora, Pump.fun y Raydium LaunchLab (la ruta usada por Stonk.fun según la etiqueta actual de Jupiter). Las consultas se ejecutan concurrentemente, con el límite de `MAX_CONCURRENT_QUOTES`. Si live está activado, reevalúa continuamente el par configurado y puede repetir operaciones hasta `MAX_TRADES_PER_HOUR`, sujeto a oportunidad, métricas y pérdida diaria.

El proyecto incluye un ejecutor live experimental de dos swaps secuenciales, además del modo paper. Antes de comprar, exige umbral de edge, histórico suficiente y aprobación estadística; luego vuelve a cotizar la venta y solo la envía si el edge esperado aún supera el mínimo. La blockchain no ofrece atomicidad entre esas dos transacciones: puede ejecutarse la compra y no la venta, dejando tokens en la wallet. En ese caso, o si la confirmación queda incierta, el proceso se detiene para reconciliación manual. No hay beneficio seguro ni garantía de ejecución rentable.

### Redes y proveedores

- **Base:** cotizaciones de 0x, Odos y Aerodrome, comparadas en ambos sentidos. Aerodrome se consulta con `getAmountsOut` en su router y requiere `RPC_URL` de Base.
- **Solana:** cotización directa mediante Raydium Trade API; Orca, Meteora, Pump.fun y LaunchLab se cotizan a través de Jupiter con filtros DEX para restringir cada ruta al protocolo correspondiente. `Stonk.fun` se mapea a la etiqueta `Raydium Launchlab`; no es una fuente de liquidez aparte de LaunchLab.
- **Laya (opcional):** servicio externo que puede actuar como veto de riesgo. No genera ni autoriza operaciones por sí solo. Véase [Laya](https://github.com/NandhaKishorM/laya).

Consulta la documentación de [0x Swap API](https://docs.0x.org/), [Odos API](https://docs.odos.xyz/build/api-docs), [Aerodrome Router](https://github.com/aerodrome-finance/contracts/blob/main/contracts/interfaces/IRouter.sol), [Raydium Trade API](https://github.com/raydium-io/raydium-docs-v1/blob/main/sdk-api/trade-api.mdx), [Jupiter DEX filters](https://github.com/jup-ag/jupiter-quote-api-node/blob/main/swagger.yaml), [Orca Whirlpools](https://github.com/orca-so/whirlpools) y [Meteora DLMM](https://github.com/MeteoraAg/docs/tree/main/developer-guides/dlmm) para conocer los requisitos vigentes.

## Requisitos

- Python 3.10 o posterior.
- Acceso a Internet para las API de cotización.
- API key de 0x. Odos usa su endpoint público de cotización, sujeto a disponibilidad y límites del servicio.
- Para Base: URL RPC HTTPS de Base en `RPC_URL` para consultar Aerodrome (la plantilla usa el RPC público de Base, que puede limitar solicitudes).
- API key de Jupiter, necesaria para cotizaciones filtradas por DEX en Solana.
- Para live: RPC privado fiable, wallet financiada con el token quote y gas nativo (ETH en Base, SOL en Solana), y claves configuradas. Usa una wallet dedicada con fondos limitados.

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

Edita `.env` con los parámetros de tu red y estrategia. El archivo `.env` está excluido del control de versiones: no publiques API keys ni otros secretos.

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
RPC_URL=https://mainnet.base.org
LIVE_TRADING=true
EVM_PRIVATE_KEY=tu_clave_privada
MAX_TRADE_AMOUNT=1000000
MAX_DAILY_LOSS=50000
MAX_TRADES_PER_HOUR=1000
MAX_GAS_PRICE_GWEI=5
```

No uses el RPC público de ejemplo como endpoint de producción. Para 0x, la ejecución usa AllowanceHolder y concede solo la aprobación exacta del importe cuando hace falta. Odos live requiere que `ODOS_ROUTER_ADDRESS` corresponda al router de Base vigente indicado por Odos. Aerodrome live solo busca un pool directo del factory por defecto. Verifica manualmente direcciones, chain y balance antes de habilitar live.

### Solana

Usa los mint addresses de los tokens de Solana y una cantidad en unidades mínimas. Por ejemplo, USDC tiene 6 decimales.

```dotenv
CHAIN=solana
QUOTE_TOKEN=<mint-del-token-de-cotizacion>
TARGET_TOKEN=<mint-del-token-objetivo>
AMOUNT_IN=1000000
JUPITER_API_KEY=tu_api_key
JUPITER_QUOTE_URL=https://api.jup.ag/swap/v1/quote
RAYDIUM_QUOTE_URL=https://transaction-v1.raydium.io/compute/swap-base-in
RPC_URL=https://tu-rpc-solana-mainnet
LIVE_TRADING=true
SOLANA_KEYPAIR_PATH=/ruta/segura/al/keypair.json
MAX_TRADE_AMOUNT=1000000
MAX_DAILY_LOSS=50000
MAX_TRADES_PER_HOUR=1000
MAX_PRIORITY_FEE_LAMPORTS=1000000
```

El archivo de keypair debe ser el JSON habitual de Solana (array de bytes). El RPC se valida contra Solana mainnet. Para live, compra y venta usan cuentas SPL existentes; configura el token quote y los fondos de SOL para comisiones. No se envuelve SOL automáticamente (`wrapSol=false`), por lo que el token nativo no sirve directamente como token quote.

### Umbrales y límites

Valores relevantes de `.env`:

| Variable | Uso |
| --- | --- |
| `AMOUNT_IN` | Cantidad de entrada en unidades mínimas del token de cotización. |
| `MIN_PROFIT_QUOTE` | Beneficio neto mínimo requerido para considerar la operación, en unidades mínimas del token de cotización. |
| `ESTIMATED_COST_QUOTE` | Reserva estimada de costes, en las mismas unidades. Ajusta este valor a la red y al tamaño de operación. |
| `SLIPPAGE_BPS` | Tolerancia al deslizamiento en puntos básicos (100 bps = 1 %). |
| `INTERVAL_SECONDS` | Pausa entre ciclos del daemon. |
| `MAX_CONCURRENT_QUOTES` | Máximo de solicitudes simultáneas de cotización. |
| `MAX_TRADES_PER_HOUR` | Límite rodante de operaciones live completadas o parciales por hora. La plantilla usa 1.000; ajusta el límite a tu capital y proveedor. |
| `DATABASE_PATH` | Ubicación de la base SQLite con el historial. |
| `RPC_URL` | RPC HTTPS de Base usado para consultar el router de Aerodrome. |
| `LIVE_TRADING` | Debe ser `true` junto con `--live` para habilitar el envío de transacciones. |
| `EVM_PRIVATE_KEY` / `SOLANA_KEYPAIR_PATH` | Credencial de la wallet correspondiente a la red. |
| `MAX_TRADE_AMOUNT` | Tope por operación en unidades mínimas del token quote; debe ser positivo en live. |
| `MAX_DAILY_LOSS` | Límite de pérdida realizada diaria en unidades mínimas; positivo y obligatorio en live. |
| `MAX_GAS_PRICE_GWEI` | Tope de precio de gas en Base. |
| `MAX_PRIORITY_FEE_LAMPORTS` | Máximo de prioridad para swaps Jupiter en Solana. |
| `ODOS_ROUTER_ADDRESS` | Dirección del router de Odos en Base que se valida antes de aprobar y enviar. |
| `JUPITER_QUOTE_URL` | Endpoint Jupiter v1 usado con filtros para Orca, Meteora, Pump.fun y LaunchLab. |
| `RAYDIUM_QUOTE_URL` | Endpoint de cotización de Raydium Trade API. |
| `METRICS_WINDOW` | Número de cotizaciones recientes usado por las métricas. |
| `METRICS_MIN_SAMPLES` | Muestras mínimas requeridas antes de habilitar el filtro estadístico. |
| `MIN_WIN_PROBABILITY` | Límite inferior de probabilidad estimada de superar el umbral de beneficio. |
| `LAYA_ENABLED` | Activa la consulta opcional al servicio Laya. |
| `LAYA_BASE_URL` | URL base de Laya; por defecto apunta al servicio local de ejemplo. |
| `LAYA_API_KEY` | Clave opcional si el servidor Laya está protegido. |
| `LAYA_BLOCK_PROBABILITY` | Umbral de riesgo comunicado por Laya a partir del cual se veta la operación. |

`SLIPPAGE_BPS` se envía a Odos para limitar el deslizamiento de la cotización. `ESTIMATED_COST_QUOTE` es una reserva adicional configurable en unidades mínimas del token quote. Debes calibrarla; el escáner no calcula por completo gas, costes de red ni coste de inventario.

## Uso

Desde el directorio del proyecto, con el entorno virtual activado:

```bash
# Un ciclo simulado
python -m bot --once

# Escáner continuo en modo paper (Ctrl+C para detenerlo)
python -m bot

# Mostrar métricas almacenadas
python -m bot --metrics
```

Live está deshabilitado por defecto. Para habilitarlo, configura todos los parámetros y límites de live, configura `MIN_PROFIT_QUOTE` y `ESTIMATED_COST_QUOTE` con unidades quote correctas, y ejecuta:

```bash
python -m bot --live --once
# Solo después de revisar los resultados y el estado de la wallet:
python -m bot --live
```

`--live` requiere además `LIVE_TRADING=true`. En modo live se aplica el veto Laya como fail-closed si `LAYA_ENABLED=true` y el servicio no responde. En una compra sin venta confirmada, en estado de transacción incierto o en cualquier excepción, el proceso se detiene. Inspecciona los hashes y balances antes de reiniciarlo. No ejecutes dos instancias con la misma wallet: el control de nonce y de exposición no es compartido entre procesos.

La plantilla usa `INTERVAL_SECONDS=2`, `MAX_CONCURRENT_QUOTES=12` y `MAX_TRADES_PER_HOUR=1000` para detectar y repetir señales con frecuencia. Son límites máximos, no una promesa de velocidad: cada arbitraje requiere que haya un edge rentable y dos transacciones confirmadas. La ejecución live de una wallet es secuencial para conservar el orden de nonce y de balances; la simultaneidad se aplica a las cotizaciones, no a los swaps de esa cuenta.

El bot guarda el historial en `DATABASE_PATH` (por defecto, `data/trades.sqlite3`). Respaldar o borrar esa base cambia las métricas históricas disponibles.

## Salida esperada

El texto exacto depende de la red, el proveedor y las cotizaciones. En un ciclo paper, el programa informa de la oportunidad y de si pasa los filtros; una salida ilustrativa sería:

```text
2026-10-12 10:00:00 INFO ARBITRAGE SCAN | mode=paper | venues=Raydium,Orca,Meteora,Pump.fun,Stonk.fun | chain=solana ...
2026-10-12 10:00:01 INFO cycle=1 status=candidate buy=Orca sell=Raydium in=... mid=... out=... estimated_net_edge=...
```

En paper solo se registra una oportunidad estimada cuando hay cotizaciones de dos proveedores distintos y la diferencia neta supera `MIN_PROFIT_QUOTE`. En live se muestran estados `live_executed`, `holding_after_buy`, `reconciliation_required` o `execution_state_unknown`; revisa PnL realizado y hashes, no el edge de cotización. El PnL live se calcula con el cambio del balance quote observado y una reserva configurada de costes; no incluye de forma exacta todos los costes de gas, prioridad, ni valor residual de tokens.

`--metrics` presenta estadísticas de las muestras almacenadas. El edge se registra en unidades mínimas del token de cotización; conviértelo a unidades legibles usando sus decimales. Las tasas estimadas se calculan a partir de cotizaciones históricas, no son una predicción garantizada del resultado de futuras operaciones.

## Métricas y filtros

El historial de cotizaciones alimenta un filtro live conservador. El filtro bloquea hasta reunir `METRICS_MIN_SAMPLES`; después requiere el límite inferior estadístico y el PnL conservador por encima de los umbrales configurados. Es una heurística basada en cotizaciones, no una probabilidad validada de fills ni un modelo predictivo. Laya es opcional y solo puede vetar señales.

Estas métricas sirven para seguimiento, no como inteligencia predictiva validada. No garantizan que las muestras sean representativas ni que una cotización se pueda ejecutar al mismo precio. Laya es opcional y puede vetar señales, pero una respuesta favorable no demuestra que la operación vaya a ser rentable.

## Límites actuales

- El escáner trabaja con el par configurado, una oportunidad por ciclo. Puede repetir ciclos continuamente y alcanzar hasta `MAX_TRADES_PER_HOUR`, pero no procesa varios tokens a la vez.
- Las operaciones live de una wallet son secuenciales. `MAX_CONCURRENT_QUOTES` paraleliza consultas, no swaps. Esta serialización evita carreras de nonce y balance; no es ejecución concurrente de órdenes.
- Las dos patas no son atómicas. El sistema se detiene ante una pata fallida o estado incierto, pero la recuperación de inventario y la reconciliación de transacciones son manuales.
- Los límites de pérdida/trades dependen de SQLite local y del reloj local; no coordinan otras instancias. Ejecuta una sola instancia por wallet y revisa la base antes de reanudar.
- El PnL realizado tiene límites: usa delta del balance quote y resta una reserva fija; algunas comisiones nativas y valoración de inventario pueden no reflejarse exactamente.
- Los agregadores pueden consultar liquidez coincidente; la diferencia puede ser solo latencia o datos desactualizados. Aerodrome solo cubre pools directos del factory por defecto, no rutas con tokens intermedios ni otros factories.
- La reserva de costes debe calibrarse manualmente y no equivale a un cálculo completo del coste de ejecución.
- Las cotizaciones de Orca, Meteora, Pump.fun y LaunchLab usan filtros DEX del endpoint Jupiter v1 y dependen de que Jupiter tenga ruta para ese token y etiqueta. La lista de etiquetas puede cambiar.
- Stonk.fun se representa mediante LaunchLab, por lo que no debe contarse como un venue separado de LaunchLab ni como comparación independiente de la misma ruta.
- El almacenamiento de métricas es local en SQLite; la pérdida o modificación de la base afecta al historial mostrado.

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
