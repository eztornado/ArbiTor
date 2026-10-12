"""Interactive discovery, candidate review, and .env setup wizard."""
import os
from pathlib import Path
import re
import stat
import tempfile
from urllib.parse import urlparse

from dotenv import dotenv_values
import requests

from .discovery import RankedToken, TokenDiscovery, rank_candidates


STABLE_SYMBOLS = {"USDC", "USDT", "USDC.E", "USDCET", "USDBC", "USDS", "DAI", "BUSD", "FDUSD", "USDE"}
DEFAULT_RPCS = {
    "base": "https://mainnet.base.org",
    "bsc": "https://bsc-dataseed.binance.org",
    "solana": "https://api.mainnet-beta.solana.com",
}
CHAIN_IDS = {"base": 8453, "bsc": 56}
TOKEN_DECIMALS_ABI = [{"type": "function", "name": "decimals", "stateMutability": "view",
                       "inputs": [], "outputs": [{"type": "uint8"}]}]


def _ask(prompt: str) -> str:
    try:
        return input(prompt).strip()
    except EOFError as exc:
        raise RuntimeError("El asistente requiere una terminal interactiva") from exc


def _choose_index(prompt: str, maximum: int, default: int = 1) -> int | None:
    value = _ask(f"{prompt} [{default}] (q para cancelar): ")
    if value.casefold() == "q":
        return None
    if not value:
        return default - 1
    try:
        index = int(value) - 1
    except ValueError:
        print("Introduce un número de la lista o q.")
        return _choose_index(prompt, maximum, default)
    if index < 0 or index >= maximum:
        print(f"Elige un número entre 1 y {maximum}.")
        return _choose_index(prompt, maximum, default)
    return index


def _choose_token_set(tokens: list[RankedToken]) -> list[RankedToken] | None:
    while True:
        value = _ask("Representaciones que confirmas como el mismo token (números separados por coma; q cancela): ")
        if value.casefold() == "q":
            return None
        try:
            indices = list(dict.fromkeys(int(part.strip()) - 1 for part in value.split(",") if part.strip()))
        except ValueError:
            print("Usa números separados por coma, por ejemplo: 1,3,4.")
            continue
        if len(indices) < 2 or any(index < 0 or index >= len(tokens) for index in indices):
            print("Selecciona al menos dos filas válidas para operar entre redes.")
            continue
        selected = [tokens[index] for index in indices]
        chains = [item.chain for item in selected]
        if len(chains) != len(set(chains)):
            print("Selecciona como máximo una representación por red.")
            continue
        return selected


def _write_env(path: Path, updates: dict[str, str]) -> None:
    if path.exists():
        content = path.read_text(encoding="utf-8")
        mode = stat.S_IMODE(path.stat().st_mode)
    else:
        template = path.with_name(".env.example")
        content = template.read_text(encoding="utf-8") if template.exists() else ""
        mode = 0o600
    lines = content.splitlines(keepends=True)
    missing = set(updates)
    replaced = []
    for line in lines:
        matched = re.match(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=", line)
        if matched and matched.group(1) in updates:
            key = matched.group(1)
            replaced.append(f"{key}={updates[key]}\n")
            missing.discard(key)
        else:
            replaced.append(line)
    if missing:
        if replaced and not replaced[-1].endswith(("\n", "\r")):
            replaced[-1] += "\n"
        if replaced and replaced[-1].strip():
            replaced.append("\n")
        replaced.extend(f"{key}={updates[key]}\n" for key in updates if key in missing)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        os.fchmod(fd, mode)
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
            handle.writelines(replaced)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temp_name, path)
    except Exception:
        try:
            os.unlink(temp_name)
        except OSError:
            pass
        raise


def _best_quote_options(token: RankedToken) -> list[dict[str, object]]:
    options: dict[str, dict[str, object]] = {}
    for pool in token.pools:
        address = pool.counter_token_address
        symbol = pool.counter_token_symbol.strip()
        if not address or not symbol:
            continue
        item = options.setdefault(address.casefold(), {
            "address": address, "symbol": symbol, "name": pool.counter_token_name,
            "liquidity": 0.0, "dexes": set(),
        })
        item["liquidity"] += pool.liquidity_usd or 0
        item["dexes"].add(pool.dex)
    values = list(options.values())
    has_stable = any(str(item["symbol"]).upper() in STABLE_SYMBOLS for item in values)
    if has_stable:
        values = [item for item in values if str(item["symbol"]).upper() in STABLE_SYMBOLS]
    values.sort(key=lambda item: float(item["liquidity"]), reverse=True)
    return values[:8]


def _prompt_quote(token: RankedToken) -> tuple[str, str] | None:
    options = _best_quote_options(token)
    print(f"\nToken de cotización sugerido para {token.chain}:{token.token_symbol}:")
    for index, item in enumerate(options, 1):
        print(f"  {index}. {item['symbol']} {item['address']} | "
              f"liq. acumulada en pools ${float(item['liquidity']):,.0f} | DEX={','.join(sorted(item['dexes']))}")
    if not options:
        print("  No se encontró un token contraparte utilizable en los pools.")
        address = _ask("Escribe manualmente el address/mint del token quote (q cancela): ")
        if address.casefold() == "q" or not address:
            return None
        symbol = _ask("Símbolo de ese token quote: ").upper()
        if not symbol:
            return None
        return address, symbol
    choice = _choose_index("Elige el token quote", len(options))
    if choice is None:
        return None
    item = options[choice]
    return str(item["address"]), str(item["symbol"])


def _rpc_for(chain: str, env: dict[str, str | None], default_chain: str) -> str:
    suffix = chain.upper()
    value = (os.getenv(f"RPC_URL_{suffix}") or env.get(f"RPC_URL_{suffix}") or "").strip()
    if not value and chain == default_chain:
        value = (os.getenv("RPC_URL") or env.get("RPC_URL") or "").strip()
    if value:
        return value
    default = DEFAULT_RPCS[chain]
    entered = _ask(f"RPC HTTPS para {chain} [{default}]: ")
    return entered or default


def _token_decimals(chain: str, token: str, rpc: str) -> int:
    parsed = urlparse(rpc)
    if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError(f"El RPC de {chain} debe usar HTTPS salvo localhost")
    if chain in CHAIN_IDS:
        from web3 import Web3
        w3 = Web3(Web3.HTTPProvider(rpc, request_kwargs={"timeout": 15}))
        if not w3.is_connected() or w3.eth.chain_id != CHAIN_IDS[chain]:
            raise RuntimeError(f"RPC no disponible o con chainId incorrecto para {chain}")
        contract = w3.eth.contract(address=w3.to_checksum_address(token), abi=TOKEN_DECIMALS_ABI)
        return int(contract.functions.decimals().call())
    response = requests.post(rpc, json={"jsonrpc": "2.0", "id": 1,
        "method": "getTokenSupply", "params": [token, {"commitment": "confirmed"}]}, timeout=15)
    response.raise_for_status()
    body = response.json()
    value = (body.get("result") or {}).get("value")
    if body.get("error") or not value:
        raise RuntimeError(f"No se pudo leer el mint en Solana: {body.get('error', body)}")
    return int(value["decimals"])


def run_wizard(query: str = "") -> bool:
    """Run the terminal wizard; return True if .env was updated."""
    print("\n=== Asistente de descubrimiento y configuración de arbitraje ===")
    if not query:
        query = _ask("1/4 Buscar token por símbolo, nombre o address/mint (q cancela): ")
        if query.casefold() == "q" or not query:
            print("Cancelado; no se modificó .env.")
            return False
    else:
        print(f"1/4 Búsqueda: {query}")

    print("\nConsultando DexScreener y CoinGecko Onchain (si está configurada su API key)...")
    candidates, errors = TokenDiscovery().discover(query)
    ranked_all = rank_candidates(candidates)
    ranked = [item for item in ranked_all if item.chain in {"base", "bsc", "solana"}]
    if errors:
        for error in errors:
            print(f"Aviso de fuente: {error}")
    if not ranked:
        print("No se encontraron candidatos en Base, BNB Chain o Solana. No se modificó .env.")
        return False

    print("\n2/4 Candidatos ordenados por score de mercado (0–100; no es probabilidad de beneficio):")
    print("Pondera liquidez 35%, volumen 25%, transacciones 15%, diversidad DEX 10%, "
          "cobertura CoinGecko multichain 10% y fuentes coincidentes 5%.")
    shown = ranked[:30]
    for index, item in enumerate(shown, 1):
        identity = item.coingecko_coin_id or "sin ID común"
        tier = "fuerte" if item.score >= 70 else "medio" if item.score >= 45 else "débil"
        print(f"{index:2}. score={item.score:02d} ({tier}) {item.chain:8} {item.token_symbol:12} "
              f"{item.token_address} | liq=${item.liquidity_usd:,.0f} "
              f"vol24h=${item.volume_24h_usd:,.0f} tx24h={item.transactions_24h} "
              f"DEX={len(item.dexes)} redes-ID={item.crosschain_count} id={identity}")
        print(f"    Señales: {'; '.join(item.reasons)}")
    if len(ranked) > len(shown):
        print(f"Mostrando {len(shown)} de {len(ranked)} candidatos; los de mayor score primero.")
    selection = _choose_token_set(shown)
    if selection is None:
        print("Cancelado; no se modificó .env.")
        return False

    coin_ids = {item.coingecko_coin_id for item in selection if item.coingecko_coin_id}
    identity_conflict = len(coin_ids) > 1
    if identity_conflict:
        print("Los candidatos seleccionados tienen CoinGecko IDs diferentes: no parecen el mismo activo.")
        confirm_identity = _ask("¿Quieres continuar bajo tu propia verificación? Escribe CONFIRMO: ")
        if confirm_identity != "CONFIRMO":
            print("Cancelado; no se modificó .env.")
            return False
    elif not all(item.coingecko_coin_id for item in selection):
        print("Al menos una dirección no tiene CoinGecko ID; el símbolo/nombre no demuestra identidad multichain.")
        confirm_identity = _ask("Confirma que revisaste y verificaste las representaciones. Escribe CONFIRMO: ")
        if confirm_identity != "CONFIRMO":
            print("Cancelado; no se modificó .env.")
            return False

    print("\n3/4 Selección y token quote por red:")
    quotes: dict[str, tuple[str, str]] = {}
    for item in selection:
        print(f"\nRepresentación {item.chain}: {item.token_symbol} {item.token_address}")
        result = _prompt_quote(item)
        if result is None:
            print("Cancelado; no se modificó .env.")
            return False
        quotes[item.chain] = result
    stable_quotes = all(symbol.upper() in STABLE_SYMBOLS for _, symbol in quotes.values())
    quote_symbols = {symbol.upper() for _, symbol in quotes.values()}
    quote_default = "USD" if stable_quotes else next(iter(quote_symbols)) if len(quote_symbols) == 1 else ""
    quote_label = _ask(f"Etiqueta económica común del quote [{quote_default or 'requerida'}]: ").upper()
    if not quote_label:
        quote_label = quote_default
    if not quote_label:
        print("Hace falta una etiqueta común para comparar PnL. No se modificó .env.")
        return False
    symbol_set = {item.token_symbol.upper() for item in selection}
    target_default = next(iter(coin_ids)).upper() if len(coin_ids) == 1 and all(item.coingecko_coin_id for item in selection) else (
        next(iter(symbol_set)) if len(symbol_set) == 1 else "")
    target_label = _ask(f"Etiqueta común del activo objetivo [{target_default or 'escribe una etiqueta'}]: ").upper()
    if not target_label:
        target_label = target_default
    if not target_label:
        print("Hace falta una etiqueta común para el token objetivo. No se modificó .env.")
        return False

    env_path = Path(".env")
    env = dotenv_values(env_path) if env_path.exists() else dotenv_values(".env.example")
    default_chain = str(env.get("CHAIN") or "base").lower()
    rpcs = {item.chain: _rpc_for(item.chain, env, default_chain) for item in selection}
    print("\nVerificando red y decimales de los tokens por RPC...")
    decimals = {}
    try:
        for item in selection:
            quote_address = quotes[item.chain][0]
            decimals[item.chain] = (
                _token_decimals(item.chain, quote_address, rpcs[item.chain]),
                _token_decimals(item.chain, item.token_address, rpcs[item.chain]),
            )
    except Exception as exc:
        print(f"No se pudo validar la configuración on-chain: {exc}\nNo se modificó .env.")
        return False
    if len({value[0] for value in decimals.values()}) != 1 or len({value[1] for value in decimals.values()}) != 1:
        print(f"Decimales incompatibles entre redes: {decimals}. El scanner no puede comparar esas unidades.")
        print("Elige mints/tokens con decimales coincidentes y vuelve a iniciar el asistente; .env no se modificó.")
        return False
    chains = [item.chain for item in selection]
    print("\n4/4 Resumen para validar:")
    print(f"  Token/alias: {target_label}; redes: {', '.join(chains)}")
    print(f"  Quote común: {quote_label}; AMOUNT_IN y límites existentes se conservarán.")
    print(f"  Identidad: {'CoinGecko ID compartido ' + next(iter(coin_ids)) if len(coin_ids) == 1 and all(item.coingecko_coin_id for item in selection) else 'validación manual del usuario'}")
    for item in selection:
        quote_address, quote_symbol = quotes[item.chain]
        rpc_host = urlparse(rpcs[item.chain]).hostname or "RPC local/configurado"
        print(f"  {item.chain}: target={item.token_address} ({item.token_symbol}); "
              f"quote={quote_address} ({quote_symbol}); RPC host={rpc_host}")
        print(f"    decimales quote/target={decimals[item.chain]}")
        print(f"    score={item.score}/100; liq=${item.liquidity_usd:,.0f}; "
              f"vol24h=${item.volume_24h_usd:,.0f}; DEX={', '.join(item.dexes)}")
        matching_pools = [pool for pool in item.pools
                          if pool.counter_token_address.casefold() == quote_address.casefold()]
        best_pool = max(matching_pools or item.pools, key=lambda pool: pool.liquidity_usd or 0)
        print(f"    pool principal={best_pool.pair_address}; fuentes={','.join(best_pool.sources)}")
        if not matching_pools:
            print("    AVISO: no se encontró un pool indexado para el quote elegido; cotiza manualmente antes de operar.")
        if best_pool.pair_url:
            print(f"    enlace={best_pool.pair_url}")
    print("  Al confirmar, el asistente configurará LIVE_TRADING=true para permitir operaciones reales.")
    print("  Después tendrás que iniciar el proceso con: python -m bot --live")
    print("  Asegúrate de que las claves y los saldos de las wallets están configurados.")
    print("  Variables de entorno exportadas en la shell pueden tener prioridad sobre los valores de .env.")
    if _ask("Si quieres empezar a arbitrar con esta configuración, escribe EMPEZAR A ARBITRAR: ") != "EMPEZAR A ARBITRAR":
        print("No se modificó .env.")
        return False

    updates = {"CHAINS": ",".join(chains), "CHAIN": chains[0], "LIVE_TRADING": "true"}
    for item in selection:
        suffix = item.chain.upper()
        updates[f"QUOTE_TOKEN_{suffix}"] = quotes[item.chain][0]
        updates[f"TARGET_TOKEN_{suffix}"] = item.token_address
        updates[f"QUOTE_ASSET_{suffix}"] = quote_label
        updates[f"TARGET_ASSET_{suffix}"] = target_label
        updates[f"RPC_URL_{suffix}"] = rpcs[item.chain]
    try:
        _write_env(env_path, updates)
    except OSError as exc:
        print(f"No se pudo guardar .env: {exc}")
        return False
    print("\nConfiguración guardada en .env con escritura atómica. LIVE_TRADING=true.")
    print("Inicia el arbitraje real con: python -m bot --live")
    print("El bot solo enviará swaps con --live; revisa AMOUNT_IN, MIN_PROFIT_QUOTE, ESTIMATED_COST_QUOTE y balances antes de iniciarlo.")
    return True
