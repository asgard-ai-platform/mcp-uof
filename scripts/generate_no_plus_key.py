import base64
from Crypto.PublicKey import RSA

def generate_no_plus_key():
    attempts = 0
    while True:
        attempts += 1
        # Generate a 512-bit RSA key (much faster to find no-plus key, and supported by .NET)
        key = RSA.generate(512)

        n = key.n
        e = key.e
        d = key.d
        p = key.p
        q = key.q
        dp = d % (p - 1)
        dq = d % (q - 1)
        inverse_q = pow(q, -1, p)

        def to_b64(val: int, size: int) -> str:
            return base64.b64encode(val.to_bytes(size, byteorder="big")).decode("ascii")

        modulus_b64 = to_b64(n, 64)
        exponent_b64 = to_b64(e, 3)
        d_b64 = to_b64(d, 64)
        p_b64 = to_b64(p, 32)
        q_b64 = to_b64(q, 32)
        dp_b64 = to_b64(dp, 32)
        dq_b64 = to_b64(dq, 32)
        inv_q_b64 = to_b64(inverse_q, 32)

        # Check if any component contains "+"
        components = [modulus_b64, exponent_b64, d_b64, p_b64, q_b64, dp_b64, dq_b64, inv_q_b64]
        if any("+" in c for c in components):
            continue

        public_xml = f"<RSAKeyValue><Modulus>{modulus_b64}</Modulus><Exponent>{exponent_b64}</Exponent></RSAKeyValue>"
        public_xml_b64 = base64.b64encode(public_xml.encode("utf-8")).decode("ascii")

        # Also check if the public_xml_b64 itself contains "+" just in case
        if "+" in public_xml_b64:
            continue

        private_xml = (
            f"<RSAKeyValue>"
            f"<Modulus>{modulus_b64}</Modulus>"
            f"<Exponent>{exponent_b64}</Exponent>"
            f"<P>{p_b64}</P>"
            f"<Q>{q_b64}</Q>"
            f"<DP>{dp_b64}</DP>"
            f"<DQ>{dq_b64}</DQ>"
            f"<InverseQ>{inv_q_b64}</InverseQ>"
            f"<D>{d_b64}</D>"
            f"</RSAKeyValue>"
        )

        print(f"Success after {attempts} attempts!")
        print("\n=== NEW PRIVATE KEY XML (NO '+' CHARACTERS) ===")
        print(private_xml)
        print("===============================================")
        print("\n=== NEW PUBLIC KEY BASE64 (NO '+' CHARACTERS) ===")
        print(public_xml_b64)
        print("=================================================")

        return public_xml_b64, private_xml

if __name__ == "__main__":
    generate_no_plus_key()
