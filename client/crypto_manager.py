import os
import base64
from cryptography.hazmat.primitives.asymmetric import rsa, padding
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

KEYS_DIR = "client/keys"


class CryptoManager:
    def __init__(self, username):
        self.username = username
        self.key_dir = os.path.join(KEYS_DIR, username)
        self.contacts_dir = os.path.join(self.key_dir, "contacts")
        os.makedirs(self.contacts_dir, exist_ok=True)
        self.private_key, self.public_key = self._load_or_generate_keys()

    def _load_or_generate_keys(self):
        priv_path = os.path.join(self.key_dir, "private.pem")
        pub_path = os.path.join(self.key_dir, "public.pem")

        if os.path.exists(priv_path) and os.path.exists(pub_path):
            with open(priv_path, "rb") as f:
                private_key = serialization.load_pem_private_key(f.read(), password=None)
            with open(pub_path, "rb") as f:
                public_key = serialization.load_pem_public_key(f.read())
        else:
            private_key = rsa.generate_private_key(
                public_exponent=65537,
                key_size=2048
            )
            public_key = private_key.public_key()
            with open(priv_path, "wb") as f:
                f.write(private_key.private_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PrivateFormat.PKCS8,
                    encryption_algorithm=serialization.NoEncryption()
                ))
            with open(pub_path, "wb") as f:
                f.write(public_key.public_bytes(
                    encoding=serialization.Encoding.PEM,
                    format=serialization.PublicFormat.SubjectPublicKeyInfo
                ))
        return private_key, public_key

    def get_public_key_b64(self):
        pem = self.public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo
        )
        return base64.b64encode(pem).decode()

    def store_peer_key(self, peer_username, b64_pubkey):
        pem = base64.b64decode(b64_pubkey)
        path = os.path.join(self.contacts_dir, f"{peer_username}.pem")
        with open(path, "wb") as f:
            f.write(pem)

    def get_peer_key(self, peer_username):
        path = os.path.join(self.contacts_dir, f"{peer_username}.pem")
        if not os.path.exists(path):
            return None
        with open(path, "rb") as f:
            return serialization.load_pem_public_key(f.read())

    def has_peer_key(self, peer_username):
        return os.path.exists(
            os.path.join(self.contacts_dir, f"{peer_username}.pem"))

    def encrypt_message(self, peer_username, plaintext):
        peer_key = self.get_peer_key(peer_username)
        if not peer_key:
            raise ValueError(f"No public key for {peer_username}")

        aes_key = AESGCM.generate_key(bit_length=256)
        nonce = os.urandom(12)
        aesgcm = AESGCM(aes_key)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode(), None)

        encrypted_aes_key = peer_key.encrypt(
            aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )

        payload = (len(encrypted_aes_key).to_bytes(2, 'big') +
                   encrypted_aes_key + nonce + ciphertext)
        return base64.b64encode(payload).decode()

    def decrypt_message(self, b64_payload):
        payload = base64.b64decode(b64_payload)
        key_len = int.from_bytes(payload[:2], 'big')
        encrypted_aes_key = payload[2:2 + key_len]
        nonce = payload[2 + key_len:2 + key_len + 12]
        ciphertext = payload[2 + key_len + 12:]

        aes_key = self.private_key.decrypt(
            encrypted_aes_key,
            padding.OAEP(
                mgf=padding.MGF1(algorithm=hashes.SHA256()),
                algorithm=hashes.SHA256(),
                label=None
            )
        )

        aesgcm = AESGCM(aes_key)
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode()
