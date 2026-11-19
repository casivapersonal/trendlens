"""Generate a VAPID key pair for web push. Run once on your computer:  python pipeline/make_vapid_keys.py
Put VAPID_PUBLIC_KEY in Vercel env vars; put VAPID_PRIVATE_KEY in GitHub repo secrets. Never share the private key."""
import base64
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives import serialization
k = ec.generate_private_key(ec.SECP256R1())
b = lambda x: base64.urlsafe_b64encode(x).rstrip(b"=").decode()
priv = k.private_numbers().private_value.to_bytes(32, "big")
pub = k.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
print("VAPID_PUBLIC_KEY =", b(pub)); print("VAPID_PRIVATE_KEY =", b(priv))
