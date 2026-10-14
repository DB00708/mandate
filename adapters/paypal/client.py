"""PayPal Orders v2 client. Works against the real sandbox (PAYPAL_MODE=sandbox) or the
bundled PayPal-compatible mock (PAYPAL_MODE=mock). Every mutating call carries a
deterministic PayPal-Request-Id so a retry returns the original result."""
import os
import re
import time

import httpx

from observability import tracing


class PayPalTimeout(Exception):
    """Request sent, response lost: the outcome is UNKNOWN."""


class PayPalUnavailable(Exception):
    """PayPal answered with 5xx or refused the connection: nothing happened (safe to retry)."""


class PayPalRejected(Exception):
    """4xx: PayPal definitively refused."""


class PayPalClient:
    def __init__(self):
        self.mode = os.getenv("PAYPAL_MODE", "mock")
        self.base = os.getenv("PAYPAL_BASE_URL", "http://paypal-mock:8002" if self.mode == "mock"
                              else "https://api-m.sandbox.paypal.com")
        self.client_id = os.getenv("PAYPAL_CLIENT_ID", "mock-client")
        self.secret = os.getenv("PAYPAL_CLIENT_SECRET", "mock-secret")
        self.timeout = float(os.getenv("PAYPAL_TIMEOUT_SECONDS", "5"))
        self.vault_id = os.getenv("PAYPAL_VAULT_ID")  # vaulted buyer token for autonomous reorders
        # Budgets are in rupees. A sandbox merchant that can't take INR charges in another currency
        # at a fixed demo rate (PAYPAL_CURRENCY=USD, PAYPAL_FX_PER_INR=0.012); policy still runs on INR.
        self.currency = os.getenv("PAYPAL_CURRENCY", "INR")
        self.fx = float(os.getenv("PAYPAL_FX_PER_INR", "1" if self.currency == "INR" else "0"))
        if self.fx <= 0:
            raise ValueError("PAYPAL_FX_PER_INR is required when PAYPAL_CURRENCY is not INR")
        self._token, self._token_exp = None, 0

    def _auth(self) -> dict:
        if not self._token or time.time() > self._token_exp - 60:
            try:
                r = httpx.post(f"{self.base}/v1/oauth2/token", auth=(self.client_id, self.secret),
                               data={"grant_type": "client_credentials"}, timeout=self.timeout)
            except httpx.TransportError as e:
                raise PayPalUnavailable(f"auth: {e}") from e
            if r.status_code >= 500:
                raise PayPalUnavailable(f"auth {r.status_code}")
            r.raise_for_status()
            body = r.json()
            self._token, self._token_exp = body["access_token"], time.time() + int(body.get("expires_in", 3600))
        return {"Authorization": f"Bearer {self._token}"}

    def _call(self, method: str, path: str, request_id: str | None = None, json: dict | None = None) -> dict:
        route = re.sub(r"/(orders|captures)/[^/]+", lambda m: f"/{m.group(1)}/{{id}}", path)
        with tracing.observe(f"PayPal {method} {route}", as_type="tool",
                             input={"path": path, "paypal_request_id": request_id, "body": json},
                             metadata={"mode": self.mode, "base": self.base}) as span:
            try:
                body = self._send(method, path, request_id, json)
            except (PayPalTimeout, PayPalUnavailable, PayPalRejected) as e:
                span.update(level="WARNING" if isinstance(e, PayPalRejected) else "ERROR",
                            status_message=f"{type(e).__name__}: {e}"[:500])
                raise
            span.update(output={"id": body.get("id"), "status": body.get("status") or body.get("verification_status")})
            return body

    def _send(self, method: str, path: str, request_id: str | None, json: dict | None) -> dict:
        headers = {**self._auth(), "Content-Type": "application/json", "Prefer": "return=representation"}
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        try:
            r = httpx.request(method, f"{self.base}{path}", headers=headers, json=json, timeout=self.timeout)
        except (httpx.ReadTimeout, httpx.RemoteProtocolError, httpx.ReadError) as e:
            raise PayPalTimeout(f"{method} {path}: {type(e).__name__}") from e
        except httpx.TransportError as e:  # connect errors: the request never landed
            raise PayPalUnavailable(f"{method} {path}: {type(e).__name__}") from e
        if r.status_code >= 500:
            raise PayPalUnavailable(f"{method} {path}: HTTP {r.status_code}")
        if r.status_code >= 400:
            raise PayPalRejected(f"{method} {path}: HTTP {r.status_code} {r.text[:300]}")
        return r.json()

    def create_order(self, request_id: str, purchase_id: str, household_id: str, total_paise: int,
                     description: str) -> dict:
        body = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "reference_id": household_id, "custom_id": purchase_id, "description": description[:127],
                "amount": self.amount(total_paise),
            }],
        }
        if self.vault_id:
            body["payment_source"] = {"paypal": {"vault_id": self.vault_id}}
        elif self.mode == "mock":
            body["payment_source"] = {"paypal": {"vault_id": "mock-vault-sharma"}}
        return self._call("POST", "/v2/checkout/orders", request_id, body)

    def capture(self, request_id: str, order_id: str) -> dict:
        return self._call("POST", f"/v2/checkout/orders/{order_id}/capture", request_id, {})

    def get_order(self, order_id: str) -> dict:
        return self._call("GET", f"/v2/checkout/orders/{order_id}")

    def refund(self, request_id: str, capture_id: str, amount_paise: int) -> dict:
        return self._call("POST", f"/v2/payments/captures/{capture_id}/refund", request_id,
                          {"amount": self.amount(amount_paise)})

    def amount(self, paise: int) -> dict:
        return {"currency_code": self.currency, "value": f"{paise / 100 * self.fx:.2f}"}

    def verify_webhook(self, headers: dict, event: dict) -> bool:
        if self.mode == "mock":
            return headers.get("x-mock-webhook-secret") == os.getenv("PAYPAL_WEBHOOK_SECRET", "mock-webhook-secret")
        body = {
            "auth_algo": headers.get("paypal-auth-algo"), "cert_url": headers.get("paypal-cert-url"),
            "transmission_id": headers.get("paypal-transmission-id"),
            "transmission_sig": headers.get("paypal-transmission-sig"),
            "transmission_time": headers.get("paypal-transmission-time"),
            "webhook_id": os.getenv("PAYPAL_WEBHOOK_ID"), "webhook_event": event,
        }
        res = self._call("POST", "/v1/notifications/verify-webhook-signature", None, body)
        return res.get("verification_status") == "SUCCESS"


def capture_id_of(order: dict) -> str | None:
    try:
        return order["purchase_units"][0]["payments"]["captures"][0]["id"]
    except (KeyError, IndexError, TypeError):
        return None


def approve_link(order: dict) -> str | None:
    for link in order.get("links", []):
        if link.get("rel") in ("approve", "payer-action"):
            return link["href"]
    return None
