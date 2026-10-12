"""Live swap execution adapters. Live execution is opt-in and guarded by config."""
import base64
import json
import time
from urllib.parse import urlparse

import requests


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


class LiveExecutor:
    def __init__(self, config):
        self.config = config
        if not config.live_trading:
            raise RuntimeError("Ejecución real desactivada: define LIVE_TRADING=true y usa --live")
        if config.max_trade_amount <= 0 or config.max_daily_loss <= 0:
            raise ValueError("LIVE requiere MAX_TRADE_AMOUNT y MAX_DAILY_LOSS mayores que cero")
        if config.amount_in > config.max_trade_amount:
            raise ValueError("AMOUNT_IN supera MAX_TRADE_AMOUNT")
        if config.min_profit_quote <= 0 or config.max_trades_per_hour <= 0:
            raise ValueError("LIVE requiere MIN_PROFIT_QUOTE y MAX_TRADES_PER_HOUR mayores que cero")
        if not 1 <= config.slippage_bps <= 300:
            raise ValueError("SLIPPAGE_BPS debe estar entre 1 y 300")
        if config.chain == "base":
            self._init_evm()
        else:
            self._init_sol()

    def _init_evm(self):
        if not self.config.rpc_url or not self.config.private_key:
            raise ValueError("Base LIVE requiere RPC_URL y EVM_PRIVATE_KEY")
        self._validate_rpc_url()
        from web3 import Web3
        self.w3 = Web3(Web3.HTTPProvider(self.config.rpc_url, request_kwargs={"timeout": 20}))
        self.account = self.w3.eth.account.from_key(self.config.private_key)
        if not self.w3.is_connected() or self.w3.eth.chain_id != 8453:
            raise RuntimeError("RPC no disponible o no corresponde a Base (chainId 8453)")
        self.wallet = self.account.address
        self.session = requests.Session()

    def _init_sol(self):
        if not self.config.rpc_url or not self.config.wallet_keypair_path:
            raise ValueError("Solana LIVE requiere RPC_URL y SOLANA_KEYPAIR_PATH")
        self._validate_rpc_url()
        from solders.keypair import Keypair
        with open(self.config.wallet_keypair_path, encoding="utf-8") as keyfile:
            raw = json.load(keyfile)
        self.keypair = Keypair.from_bytes(bytes(raw))
        self.wallet = str(self.keypair.pubkey())
        self.session = requests.Session()

    def _validate_rpc_url(self):
        parsed = urlparse(self.config.rpc_url)
        if parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"}:
            raise ValueError("RPC_URL debe usar HTTPS salvo en localhost")

    def cycle(self, quote_client):
        if self.config.chain == "base":
            return self._cycle_evm(quote_client)
        return self._cycle_sol(quote_client)

    def _evm_token(self, address):
        return self.w3.eth.contract(address=self.w3.to_checksum_address(address), abi=ERC20_ABI)

    def _execute_0x_leg(self, sell, buy, amount):
        response = self.session.get(
            "https://api.0x.org/swap/allowance-holder/quote",
            params={"chainId": 8453, "sellToken": sell, "buyToken": buy,
                    "sellAmount": str(amount), "taker": self.wallet,
                    "slippageBps": self.config.slippage_bps},
            headers={"0x-api-key": self.config.zerox_api_key, "0x-version": "v2"}, timeout=20,
        )
        response.raise_for_status()
        quote = response.json()
        txq = quote.get("transaction") or {}
        if not quote.get("liquidityAvailable") or not txq.get("to") or not txq.get("data"):
            raise RuntimeError("0x no devolvió una cotización firme ejecutable")
        if int(quote.get("sellAmount", "0")) != amount:
            raise RuntimeError("La cotización 0x cambió el importe de entrada")
        if self.w3.to_checksum_address(quote.get("sellToken", "")) != self.w3.to_checksum_address(sell):
            raise RuntimeError("La cotización 0x no coincide con el token de venta esperado")
        if self.w3.to_checksum_address(quote.get("buyToken", "")) != self.w3.to_checksum_address(buy):
            raise RuntimeError("La cotización 0x no coincide con el token de compra esperado")

        issues = quote.get("issues") or {}
        allowance_issue = issues.get("allowance") or {}
        spender = allowance_issue.get("spender") or quote.get("allowanceTarget")
        tx_to = self.w3.to_checksum_address(txq["to"])
        if spender and self.w3.to_checksum_address(spender) != tx_to:
            raise RuntimeError("El spender y el destino de la transacción 0x no coinciden")
        if int(txq.get("value", "0")) != 0:
            raise RuntimeError("Se rechazó valor nativo inesperado en una operación ERC-20")
        if spender:
            token = self._evm_token(sell)
            spender = self.w3.to_checksum_address(spender)
            allowed = token.functions.allowance(self.wallet, spender).call()
            if allowed < amount:
                approval = token.functions.approve(spender, amount).build_transaction({
                    "from": self.wallet, "nonce": self.w3.eth.get_transaction_count(self.wallet),
                    "chainId": 8453, "gas": 100000, "gasPrice": self.w3.eth.gas_price,
                })
                signed = self.account.sign_transaction(approval)
                approval_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
                approval_receipt = self.w3.eth.wait_for_transaction_receipt(approval_hash, timeout=90)
                if approval_receipt.status != 1:
                    raise RuntimeError("La aprobación ERC-20 falló")

        # Never trust API-provided sender, chain id, nonce or value; build a bounded tx for Base.
        to = tx_to
        transaction = {
            "from": self.wallet, "to": to, "data": txq["data"],
            "value": int(txq.get("value", "0")),
            "nonce": self.w3.eth.get_transaction_count(self.wallet), "chainId": 8453,
            "gas": min(int(txq.get("gas", 0) or 0) * 12 // 10, 2_000_000) or 500_000,
            "gasPrice": min(int(txq.get("gasPrice", 0) or self.w3.eth.gas_price), self.w3.eth.gas_price * 3),
        }
        # Simulate before signing/broadcasting to catch stale or invalid calldata.
        self.w3.eth.call({k: v for k, v in transaction.items() if k in {"from", "to", "data", "value"}})
        signed = self.account.sign_transaction(transaction)
        tx_hash = self.w3.eth.send_raw_transaction(signed.raw_transaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=120)
        if receipt.status != 1:
            raise RuntimeError(f"Swap EVM revertido: {tx_hash.hex()}")
        return tx_hash.hex(), int(quote["buyAmount"])

    def _cycle_evm(self, quote_client):
        before_q = self._evm_token(self.config.quote_token).functions.balanceOf(self.wallet).call()
        before_t = self._evm_token(self.config.target_token).functions.balanceOf(self.wallet).call()
        if self.config.amount_in > self.config.max_trade_amount:
            raise RuntimeError("AMOUNT_IN supera MAX_TRADE_AMOUNT")
        if before_q < self.config.amount_in:
            raise RuntimeError("Saldo insuficiente del token de cotización")
        buy_hash, _ = self._execute_0x_leg(self.config.quote_token, self.config.target_token,
                                           self.config.amount_in)
        after_buy_t = self._evm_token(self.config.target_token).functions.balanceOf(self.wallet).call()
        received = after_buy_t - before_t
        if received <= 0:
            raise RuntimeError(f"Compra confirmada pero no se detectó token recibido: {buy_hash}")
        expected = quote_client.quote(self.config.target_token, self.config.quote_token, received).amount_out
        pnl_estimate = expected - self.config.amount_in - self.config.estimated_cost_quote
        if pnl_estimate < self.config.min_profit_quote:
            return {"status": "holding_after_buy", "pnl": -self.config.amount_in,
                    "details": f"Compra {buy_hash}; no se vendió porque la nueva cotización no alcanza el umbral"}
        sell_hash, _ = self._execute_0x_leg(self.config.target_token, self.config.quote_token, received)
        after_q = self._evm_token(self.config.quote_token).functions.balanceOf(self.wallet).call()
        actual_out = after_q - before_q
        return {"status": "executed", "pnl": actual_out - self.config.estimated_cost_quote,
                "details": f"buy={buy_hash} sell={sell_hash}; PnL on-chain en unidades quote"}

    def _rpc(self, method, params):
        response = self.session.post(self.config.rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}, timeout=20)
        response.raise_for_status()
        body = response.json()
        if "error" in body:
            raise RuntimeError(f"Solana RPC {method}: {body['error']}")
        return body["result"]

    def _sol_token_balance(self, mint):
        result = self._rpc("getTokenAccountsByOwner", [self.wallet, {"mint": mint}, {"encoding": "jsonParsed"}])
        return sum(int(row["account"]["data"]["parsed"]["info"]["tokenAmount"]["amount"])
                   for row in result["value"])

    def _sol_balance(self, mint):
        # Native SOL is represented by its wrapped-SOL mint in Jupiter routes.
        if mint == "So11111111111111111111111111111111111111112":
            return int(self._rpc("getBalance", [self.wallet, {"commitment": "confirmed"}])["value"])
        return self._sol_token_balance(mint)

    def _execute_jupiter_leg(self, quote_body):
        from solders.transaction import VersionedTransaction
        headers = {"Content-Type": "application/json"}
        if not self.config.jupiter_api_key:
            raise ValueError("Jupiter LIVE requiere JUPITER_API_KEY")
        headers["x-api-key"] = self.config.jupiter_api_key
        order_response = self.session.get(f"{self.config.jupiter_base_url}/order",
            params={"inputMint": quote_body["inputMint"], "outputMint": quote_body["outputMint"],
                    "amount": quote_body["amount"], "taker": self.wallet},
            headers=headers, timeout=20)
        order_response.raise_for_status()
        order = order_response.json()
        if not order.get("transaction"):
            raise RuntimeError(f"Jupiter no pudo construir orden: {order.get('errorMessage', order)}")
        transaction = VersionedTransaction.from_bytes(base64.b64decode(order["transaction"]))
        transaction.sign([self.keypair])
        signed_tx = base64.b64encode(bytes(transaction)).decode("ascii")
        execution = self.session.post(f"{self.config.jupiter_base_url}/execute",
            json={"signedTransaction": signed_tx, "requestId": order["requestId"]},
            headers=headers, timeout=90)
        execution.raise_for_status()
        result = execution.json()
        if result.get("status") != "Success" or int(result.get("code", -1)) != 0:
            raise RuntimeError(f"Jupiter swap falló: {result.get('error', result)}")
        return (result["signature"], int(result["totalInputAmount"]),
                int(result["totalOutputAmount"]))

    def _cycle_sol(self, quote_client):
        amount = self.config.amount_in
        if amount > self.config.max_trade_amount:
            raise RuntimeError("AMOUNT_IN supera MAX_TRADE_AMOUNT")
        before_q = self._sol_balance(self.config.quote_token)
        if before_q < amount:
            raise RuntimeError("Saldo insuficiente del token de cotización")
        buy = {"inputMint": self.config.quote_token, "outputMint": self.config.target_token,
               "amount": str(amount)}
        buy_sig, actual_input, received = self._execute_jupiter_leg(buy)
        if received <= 0:
            raise RuntimeError(f"Compra confirmada pero no se detectó token: {buy_sig}")
        sell_quote = {"inputMint": self.config.target_token, "outputMint": self.config.quote_token,
                      "amount": str(received)}
        pnl_estimate = quote_client.quote(self.config.target_token, self.config.quote_token, received).amount_out - amount - self.config.estimated_cost_quote
        if pnl_estimate < self.config.min_profit_quote:
            return {"status": "holding_after_buy", "pnl": -amount,
                    "details": f"Compra {buy_sig}; la cotización de venta cayó bajo el umbral"}
        sell_sig, _, actual_output = self._execute_jupiter_leg(sell_quote)
        return {"status": "executed", "pnl": actual_output - actual_input - self.config.estimated_cost_quote,
                "details": f"buy={buy_sig} sell={sell_sig}; PnL on-chain en unidades quote"}
