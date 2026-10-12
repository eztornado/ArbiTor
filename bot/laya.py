"""Optional qualitative risk veto via Laya's documented Jev-compatible API."""
from urllib.parse import urlparse

import requests


class LayaRiskVeto:
    def __init__(self, config):
        self.base_url = config.laya_base_url.rstrip("/")
        self.block_probability = config.laya_block_probability
        self.session = requests.Session()
        self.headers = {"Content-Type": "application/json"}
        if config.laya_api_key:
            self.headers["Authorization"] = f"Bearer {config.laya_api_key}"
        parsed = urlparse(self.base_url)
        private_vpn = bool(parsed.hostname and parsed.hostname.endswith(".netbird.vpn"))
        if not self.base_url or (parsed.scheme != "https" and parsed.hostname not in {"localhost", "127.0.0.1"} and not private_vpn):
            raise ValueError("LAYA_BASE_URL debe usar HTTPS o una dirección local/NetBird")

    def assess(self, snapshot: dict) -> tuple[float, bool]:
        response = self.session.post(
            f"{self.base_url}/v1/systemone",
            json={
                "state": {"market_snapshot": snapshot},
                "questions": {
                    "block_risk": {
                        "type": "noul",
                        "instructions": "¿Hay señales suficientes en estos datos para BLOQUEAR la operación por riesgo? Evalúa inestabilidad, poca evidencia histórica, spread o pnl ajustado insuficientes. Bloquea ante incertidumbre; no intentes predecir precios.",
                        "criteria": {
                            "true": "La operación debe bloquearse porque existe riesgo o falta evidencia confiable.",
                            "false": "No se observa una señal cualitativa adicional para bloquearla.",
                        },
                        "labels": {"true": "BLOCK", "false": "CLEAR"},
                    }
                },
            },
            headers=self.headers,
            timeout=8,
        )
        response.raise_for_status()
        body = response.json()
        try:
            probability = float(body["answers"]["block_risk"]["noul"])
        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError("Respuesta Laya no contiene answers.block_risk.noul") from exc
        if not 0 <= probability <= 1:
            raise RuntimeError("Laya devolvió probabilidad fuera del intervalo [0,1]")
        return probability, probability >= self.block_probability

    def health(self) -> bool:
        response = self.session.get(f"{self.base_url}/health", headers=self.headers, timeout=3)
        return response.ok
