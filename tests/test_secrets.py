import unittest

from app.core.secrets import derive_key

try:
    import cryptography  # noqa: F401
    HAVE_CRYPTO = True
except ImportError:
    HAVE_CRYPTO = False


class KdfTests(unittest.TestCase):
    def test_key_is_deterministic(self):
        self.assertEqual(derive_key("master-key"), derive_key("master-key"))

    def test_different_keys_differ(self):
        self.assertNotEqual(derive_key("a"), derive_key("b"))

    def test_key_is_fernet_shaped(self):
        # urlsafe-base64 of 32 bytes -> 44 chars.
        self.assertEqual(len(derive_key("x")), 44)

    def test_empty_key_rejected(self):
        with self.assertRaises(ValueError):
            derive_key("")


@unittest.skipUnless(HAVE_CRYPTO, "cryptography not installed")
class SecretBoxTests(unittest.TestCase):
    def test_seal_open_roundtrip(self):
        from app.core.secrets import SecretBox

        box = SecretBox("master-key")
        token = box.seal("hunter2")
        self.assertNotIn(b"hunter2", token)  # not plaintext
        self.assertEqual(box.open(token), "hunter2")

    def test_wrong_key_cannot_open(self):
        from cryptography.fernet import InvalidToken

        from app.core.secrets import SecretBox

        token = SecretBox("key-a").seal("secret")
        with self.assertRaises(InvalidToken):
            SecretBox("key-b").open(token)


if __name__ == "__main__":
    unittest.main()
