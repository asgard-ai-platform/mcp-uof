import base64
from Crypto.PublicKey import RSA

def generate_dotnet_rsa_keys():
    # Generate a 1024-bit RSA key (standard for UOF)
    key = RSA.generate(1024)

    # Extract components
    n = key.n
    e = key.e
    d = key.d
    p = key.p
    q = key.q

    # Calculate DP, DQ, InverseQ
    dp = d % (p - 1)
    dq = d % (q - 1)
    # PyCryptodome computes inverse of q mod p
    # In .NET RSAParameters, InverseQ is q^-1 mod p
    inverse_q = pow(q, -1, p)

    # Helper to convert int to base64 bytes
    def to_b64(val: int, size: int) -> str:
        # Convert to big-endian bytes
        b = val.to_bytes(size, byteorder="big")
        return base64.b64encode(b).decode("ascii")

    # Size constants for 1024-bit RSA
    modulus_b64 = to_b64(n, 128)
    exponent_b64 = to_b64(e, 3) # exponent is usually 3 bytes (65537)
    d_b64 = to_b64(d, 128)
    p_b64 = to_b64(p, 64)
    q_b64 = to_b64(q, 64)
    dp_b64 = to_b64(dp, 64)
    dq_b64 = to_b64(dq, 64)
    inv_q_b64 = to_b64(inverse_q, 64)

    # Construct XML strings (without whitespace or formatting to be clean)
    public_xml = f"<RSAKeyValue><Modulus>{modulus_b64}</Modulus><Exponent>{exponent_b64}</Exponent></RSAKeyValue>"

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

    # UOF_RSA_PUBLIC_KEY in .env expects the Base64 of the public XML string
    public_xml_b64 = base64.b64encode(public_xml.encode("utf-8")).decode("ascii")

    print("=== UOF RSA KEY GENERATOR ===")
    print("\n[UOF System Configuration]")
    print("Please copy the following PRIVATE KEY XML and paste it into the UOF API configuration (外部對應之私鑰):")
    print("-" * 80)
    print(private_xml)
    print("-" * 80)

    print("\n[MCP Server configuration (.env)]")
    print("The UOF_RSA_PUBLIC_KEY value (Base64 of XML public key) is:")
    print("-" * 80)
    print(public_xml_b64)
    print("-" * 80)

    return public_xml_b64, private_xml

if __name__ == "__main__":
    generate_dotnet_rsa_keys()
