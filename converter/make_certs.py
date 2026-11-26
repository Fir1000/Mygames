"""Write a CA bundle = pip's normal bundle + the Windows certificate store.

Antivirus "HTTPS scanning" (Avast, AVG, Kaspersky, ESET...) re-signs traffic with its own
root that only exists in the Windows store, so pip's built-in bundle rejects it.
Verification stays on; we just also trust what Windows already trusts.
"""
import ssl
import sys

out = sys.argv[1]
parts = []
try:
    from pip._vendor import certifi
    with open(certifi.where(), encoding="utf-8") as f:
        parts.append(f.read())
except Exception:
    pass
if sys.platform == "win32":
    for store in ("ROOT", "CA"):
        for cert, enc, trust in ssl.enum_certificates(store):
            if enc == "x509_asn":
                parts.append(ssl.DER_cert_to_PEM_cert(cert))
with open(out, "w", encoding="utf-8") as f:
    f.write("\n".join(parts))
