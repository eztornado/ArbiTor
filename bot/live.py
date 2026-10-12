"""Sequential, balance-verified execution adapters for live two-leg swaps."""
import base64
import json
import time
from urllib.parse import urlparse

import requests

from .quotes import AERODROME_ROUTER, AERODROME_ROUTER_ABI


ERC20_ABI = [
    {"type": "function", "name": "balanceOf", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}], "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "allowance", "stateMutability": "view",
     "inputs": [{"name": "owner", "type": "address"}, {"name": "spender", "type": "address"}],
     "outputs": [{"type": "uint256"}]},
    {"type": "function", "name": "approve", "stateMutability": "nonpayable",
     "inputs": [{"name": "spender", "type": "address"}, {"name": "amount", "type": "uint256"}],
     "outputs": [{"type": "bool"}]},
]

DEXES = {
    "Orca": ["Orca V1", "Orca V2", "Whirlpool"],
    "Meteora": ["Meteora", "Meteora DLMM", "Meteora DAMM v2"],
    "Pump.fun": ["Pump.fun", "Pump.fun Amm"],
    "Stonk.fun": ["Raydium Launchlab"],
}


class LiveExecutor:
    """Executes one pair of swaps at a time and halts on any partial trade."""

    def __init__(self, config):
        self.config = config
        if not config.live_trading:
            raise RuntimeError("Live requiere LIVE_TRADING=true y el argumento --live")
        if config.amount_in > config.max_trade_amount or config.max_trade_amount <= 0:
            raise ValueError("LIVE requiere MAX_TRADE_AMOUNT >= AMOUNT_IN y mayor que cero")
        if config.max_daily_loss <= 0 or config.max_trades_per_hour <= 0:
            raise ValueError("LIVE requiere MAX_DAILY_LOSS y MAX_TRADES_PER_HOUR positivos")
        if config.min_profit_quote <= 0 or config.max_priority_fee_lamports <= 0:
            raise ValueError("LIVE requiere MIN_PROFIT_QUOTE y MAX_PRIORITY_FEE_LAMPORTS positivos")
        if not 1 <= config.slippage_bps <= 300:
            raise ValueError("SLIPPAGE_BPS debe estar entre 1 y 300")
        parsed = urlparse(config.rpc_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("RPC_URL debe usar HTTPS salvo localhost")
        self.session = requests.Session()
        if config.chain == "base":
            self._init_evm()
        else:
            self._init_solana()

    def _init_evm(self):
        if not self.config.private_key or not self.config.zerox_api_key:
            raise ValueError("Base live requiere EVM_PRIVATE_KEY y ZEROX_API_KEY")
        from web3 import Web3
        self.w3 = Web3(Web3.HTTPProvider(self.config.rpc_url, request_kwargs={"timeout": 20}))
        if not self.w3.is_connected() or self.w3.eth.chain_id != 8453:
            raise RuntimeError("RPC_URL no disponible o no corresponde a Base (chainId 8453)")
        self.account = self.w3.eth.account.from_key(self.config.private_key)
        self.wallet = self.account.address
        self.aero = self.w3.eth.contract(
            address=self.w3.to_checksum_address(AERODROME_ROUTER), abi=AERODROME_ROUTER_ABI)

    def _init_solana(self):
        if not self.config.solana_keypair_path or not self.config.jupiter_api_key:
            raise ValueError("Solana live requiere SOLANA_KEYPAIR_PATH y JUPITER_API_KEY")
        from solders.keypair import Keypair
        with open(self.config.solana_keypair_path, encoding="utf-8") as handle:
            self.keypair = Keypair.from_bytes(bytes(json.load(handle)))
        self.wallet = str(self.keypair.pubkey())
        genesis = self._sol_rpc("getGenesisHash", [])
        if genesis != "5eykt4UsFv8P8NJdTREpY1vzqKqZKvdp":
            raise RuntimeError("RPC_URL no corresponde a Solana mainnet")

    def _evm_token(self, mint):
        return self.w3.eth.contract(address=self.w3.to_checksum_address(mint), abi=ERC20_ABI)

    def _approve(self, token_address, spender, amount):
        token = self._evm_token(token_address)
        spender = self.w3.to_checksum_address(spender)
        if token.functions.allowance(self.wallet, spender).call() >= amount:
            return
        self._check_gas_price()
        tx = token.functions.approve(spender, amount).build_transaction({
            "from": self.wallet,
            "nonce": self.w3.eth.get_transaction_count(self.wallet, "pending"),
            "chainId": 8453,
            "gas": 100_000,
            "gasPrice": self.w3.eth.gas_price,
        })
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=90)
        if receipt.status != 1:
            raise RuntimeError(f"Aprobación ERC-20 fallida: {tx_hash.hex()}")

    def _send_evm(self, to, data, value=0, gas_hint=0):
        self._check_gas_price()
        if int(value) != 0:
            raise RuntimeError("LIVE ERC-20 no permite value nativo inesperado")
        base_gas = self.w3.eth.gas_price
        tx = {"from": self.wallet, "to": self.w3.to_checksum_address(to), "data": data,
              "value": int(value), "nonce": self.w3.eth.get_transaction_count(self.wallet, "pending"),
              "chainId": 8453, "gasPrice": base_gas}
        estimate = self.w3.eth.estimate_gas(tx)
        if gas_hint:
            estimate = max(estimate, int(gas_hint))
        tx["gas"] = min(estimate * 120 // 100, 2_000_000)
        self.w3.eth.call({k: tx[k] for k in ("from", "to", "data", "value")})
        signed = self.account.sign_transaction(tx)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"Swap Base revertido: {tx_hash.hex()}")
        return tx_hash.hex()

    def _check_gas_price(self):
        limit = int(self.config.max_gas_price_gwei * 1_000_000_000)
        if self.w3.eth.gas_price > limit:
            raise RuntimeError("Precio de gas de Base por encima de MAX_GAS_PRICE_GWEI")

    def _swap_0x(self, sell, buy, amount):
        response = self.session.get("https://api.0x.org/swap/allowance-holder/quote", params={
            "chainId": 8453, "sellToken": sell, "buyToken": buy,
            "sellAmount": str(amount), "taker": self.wallet,
        }, headers={"0x-api-key": self.config.zerox_api_key, "0x-version": "v2"}, timeout=20)
        response.raise_for_status()
        quote = response.json()
        tx = quote.get("transaction") or {}
        if not quote.get("liquidityAvailable") or not tx.get("to") or not tx.get("data"):
            raise RuntimeError("0x no devolvió quote ejecutable")
        if int(quote.get("sellAmount", "0")) != amount:
            raise RuntimeError("Quote 0x cambió el importe de entrada")
        if self.w3.to_checksum_address(quote.get("sellToken", "")) != self.w3.to_checksum_address(sell):
            raise RuntimeError("Quote 0x no coincide con el token de entrada")
        if self.w3.to_checksum_address(quote.get("buyToken", "")) != self.w3.to_checksum_address(buy):
            raise RuntimeError("Quote 0x no coincide con el token de salida")
        spender = ((quote.get("issues") or {}).get("allowance") or {}).get("spender") or quote.get("allowanceTarget")
        if not spender:
            raise RuntimeError("0x no devolvió el spender para aprobar el token")
        if self.w3.to_checksum_address(spender) != self.w3.to_checksum_address(tx["to"]):
            raise RuntimeError("0x AllowanceHolder spender no coincide con transaction.to")
        self._approve(sell, spender, amount)
        return self._send_evm(tx["to"], tx["data"], tx.get("value", 0), tx.get("gas", 0))

    def _swap_odos(self, sell, buy, amount):
        if not self.config.odos_router_address:
            raise RuntimeError("Odos LIVE requiere ODOS_ROUTER_ADDRESS verificado para Base")
        quote_response = self.session.post("https://api.odos.xyz/sor/quote/v2", json={
            "chainId": 8453, "inputTokens": [{"tokenAddress": sell, "amount": str(amount)}],
            "outputTokens": [{"tokenAddress": buy, "proportion": 1}],
            "slippageLimitPercent": self.config.slippage_bps / 100,
            "userAddr": self.wallet, "compact": True,
        }, timeout=20)
        quote_response.raise_for_status()
        quote = quote_response.json()
        if not quote.get("pathId") or int((quote.get("inAmounts") or ["0"])[0]) != amount:
            raise RuntimeError("Odos no devolvió una ruta ejecutable exacta")
        assembly_response = self.session.post("https://api.odos.xyz/sor/assemble", json={
            "userAddr": self.wallet, "pathId": quote["pathId"], "simulate": True,
        }, timeout=20)
        assembly_response.raise_for_status()
        assembled = assembly_response.json()
        tx = assembled.get("transaction") or {}
        if not tx.get("to") or not tx.get("data"):
            raise RuntimeError(f"Odos no construyó transacción: {assembled}")
        if self.w3.to_checksum_address(tx["to"]) != self.w3.to_checksum_address(self.config.odos_router_address):
            raise RuntimeError("Odos devolvió un router distinto de ODOS_ROUTER_ADDRESS")
        spender = self.config.odos_router_address
        self._approve(sell, spender, amount)
        return self._send_evm(tx["to"], tx["data"], tx.get("value", 0), tx.get("gas", 0))

    def _swap_aerodrome(self, sell, buy, amount):
        factory = self.aero.functions.defaultFactory().call()
        best = None
        for stable in (False, True):
            route = [(self.w3.to_checksum_address(sell), self.w3.to_checksum_address(buy), stable, factory)]
            try:
                out = int(self.aero.functions.getAmountsOut(amount, route).call()[-1])
                if best is None or out > best[0]:
                    best = (out, stable, route)
            except Exception:
                continue
        if not best:
            raise RuntimeError("Aerodrome no tiene pool directo ejecutable")
        out, _, routes = best
        min_out = out * (10_000 - self.config.slippage_bps) // 10_000
        self._approve(sell, AERODROME_ROUTER, amount)
        fn = self.aero.functions.swapExactTokensForTokens(
            amount, min_out, routes, self.wallet, int(time.time()) + 60)
        self._check_gas_price()
        built = fn.build_transaction({
            "from": self.wallet,
            "nonce": self.w3.eth.get_transaction_count(self.wallet, "pending"),
            "chainId": 8453, "gas": 800_000, "gasPrice": self.w3.eth.gas_price,
        })
        return self._send_evm(built["to"], built["data"], 0, built["gas"])

    def _sol_rpc(self, method, params):
        response = self.session.post(self.config.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=25)
        response.raise_for_status()
        body = response.json()
        if body.get("error"):
            raise RuntimeError(f"Solana RPC {method}: {body['error']}")
        return body["result"]

    def _sol_balance(self, mint):
        result = self._sol_rpc("getTokenAccountsByOwner", [
            self.wallet, {"mint": mint}, {"encoding": "jsonParsed", "commitment": "confirmed"}])
        return sum(int(row["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                   for row in result["value"])

    def _send_sol_transaction(self, encoded):
        from solders.transaction import VersionedTransaction
        transaction = VersionedTransaction.from_bytes(base64.b64decode(encoded))
        transaction.sign([self.keypair])
        wire = base64.b64encode(bytes(transaction)).decode("ascii")
        signature = self._sol_rpc("sendTransaction", [wire, {"encoding": "base64", "preflightCommitment": "confirmed", "maxRetries": 3}])
        for _ in range(60):
            statuses = self._sol_rpc("getSignatureStatuses", [[signature], {"searchTransactionHistory": True}])
            status = statuses["value"][0]
            if status and status.get("err"):
                raise RuntimeError(f"Swap Solana fallido {signature}: {status['err']}")
            if status and status.get("confirmationStatus") in {"confirmed", "finalized"}:
                return signature
            time.sleep(1)
        raise TimeoutError(f"No se confirmó la transacción Solana {signature}")

    def _jupiter_quote(self, provider, sell, buy, amount):
        import logging
        dexes = DEXES[provider]
        response = self.session.get(self.config.jupiter_quote_url, params={
            "inputMint": sell, "outputMint": buy, "amount": str(amount),
            "slippageBps": str(self.config.slippage_bps), "dexes": ",".join(dexes),
        }, headers={"x-api-key": self.config.jupiter_api_key}, timeout=20)
        response.raise_for_status()
        quote = response.json()
        labels = {step.get("swapInfo", {}).get("label") for step in quote.get("routePlan", [])}
        if not quote.get("outAmount") or not labels or not labels.issubset(set(dexes)):
            raise RuntimeError(f"Jupiter no construyó ruta {provider} restringida: {quote}")
        return quote

    def _swap_jupiter_dex(self, provider, sell, buy, amount):
        quote = self._jupiter_quote(provider, sell, buy, amount)
        response = self.session.post("https://api.jup.ag/swap/v1/swap", json={
            "userPublicKey": self.wallet, "quoteResponse": quote,
            "wrapAndUnwrapSol": False, "dynamicComputeUnitLimit": True,
            "prioritizationFeeLamports": {"priorityLevelWithMaxLamports": {
                "priorityLevel": "high", "maxLamports": self.config.max_priority_fee_lamports}},
        }, headers={"x-api-key": self.config.jupiter_api_key}, timeout=25)
        response.raise_for_status()
        body = response.json()
        if not body.get("swapTransaction"):
            raise RuntimeError(f"Jupiter no construyó swap: {body}")
        return self._send_sol_transaction(body["swapTransaction"])

    def _swap_raydium(self, sell, buy, amount):
        quote_response = self.session.get(self.config.raydium_quote_url, params={
            "inputMint": sell, "outputMint": buy, "amount": str(amount),
            "slippageBps": str(self.config.slippage_bps), "txVersion": "V0",
        }, timeout=20)
        quote_response.raise_for_status()
        quote = quote_response.json()
        if not quote.get("success") or not (quote.get("data") or {}).get("outputAmount"):
            raise RuntimeError(f"Raydium no devolvió cotización: {quote}")
        built_response = self.session.post("https://transaction-v1.raydium.io/transaction/swap-base-in", json={
            "computeUnitPriceMicroLamports": "50000", "swapResponse": quote,
            "txVersion": "V0", "wallet": self.wallet,
            "wrapSol": False,
            "unwrapSol": False,
        }, timeout=25)
        built_response.raise_for_status()
        body = built_response.json()
        txs = (body.get("data") or [])
        if not body.get("success") or not txs:
            raise RuntimeError(f"Raydium no construyó la transacción: {body}")
        signature = None
        for item in txs:
            signature = self._send_sol_transaction(item["transaction"])
        return signature

    def balance(self, token):
        if self.config.chain == "base":
            return int(self._evm_token(token).functions.balanceOf(self.wallet).call())
        return self._sol_balance(token)

    def swap(self, provider, sell, buy, amount):
        before = self.balance(buy)
        if self.config.chain == "base":
            if provider == "0x":
                signature = self._swap_0x(sell, buy, amount)
            elif provider == "Odos":
                signature = self._swap_odos(sell, buy, amount)
            elif provider == "Aerodrome":
                signature = self._swap_aerodrome(sell, buy, amount)
            else:
                raise ValueError(f"Proveedor {provider} no compatible con Base")
        else:
            if provider == "Raydium":
                signature = self._swap_raydium(sell, buy, amount)
            elif provider in DEXES:
                signature = self._swap_jupiter_dex(provider, sell, buy, amount)
            else:
                raise ValueError(f"Proveedor {provider} no compatible con Solana")
        received = self.balance(buy) - before
        if received <= 0:
            raise RuntimeError(f"Swap confirmado sin balance recibido ({signature})")
        return signature, received

    def execute_opportunity(self, opportunity, quote_client):
        """Buy, refresh the exit quote, then sell; partial fills are reported for manual recovery."""
        if opportunity.amount_in > self.config.max_trade_amount:
            raise RuntimeError("La oportunidad supera MAX_TRADE_AMOUNT")
        quote_before = self.balance(self.config.quote_token)
        try:
            buy_signature, received = self.swap(
                opportunity.buy_provider, self.config.quote_token,
                self.config.target_token, opportunity.amount_in)
        except Exception as exc:
            return {"status": "execution_state_unknown", "pnl": 0,
                    "details": f"No se pudo confirmar la pata de compra; revisar wallet y transacciones: {exc}"}
        try:
            fresh_sell = quote_client.quote_from(
                opportunity.sell_provider, self.config.target_token,
                self.config.quote_token, received)
            expected_net = fresh_sell.amount_out - opportunity.amount_in - self.config.estimated_cost_quote
            if expected_net < self.config.min_profit_quote:
                return {"status": "holding_after_buy", "pnl": 0, "received": received,
                        "buy_signature": buy_signature,
                        "details": f"Compra confirmada {buy_signature}; el edge de salida cayó a {expected_net}"}
            sell_signature, sold_amount = self.swap(
                opportunity.sell_provider, self.config.target_token,
                self.config.quote_token, received)
            quote_after = self.balance(self.config.quote_token)
            pnl = quote_after - quote_before - self.config.estimated_cost_quote
            return {"status": "live_executed", "pnl": pnl, "received": received,
                    "buy_signature": buy_signature, "sell_signature": sell_signature,
                    "sold_amount": sold_amount,
                    "details": f"buy={buy_signature} sell={sell_signature}; realized quote balance delta"}
        except Exception as exc:
            return {"status": "reconciliation_required", "pnl": 0, "received": received,
                    "buy_signature": buy_signature,
                    "details": f"Compra confirmada {buy_signature}; estado de venta incierto, revisar wallet y transacciones: {exc}"}
