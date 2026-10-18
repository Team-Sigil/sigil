# Trust roots

Fetched 2026-10-18 over HTTPS from the vendors' official pages. Re-check the fingerprints
against those pages before relying on them.

| File | Source | SHA-256 fingerprint |
|---|---|---|
| `apple_app_attestation_root_ca.pem` | https://www.apple.com/certificateauthority/Apple_App_Attestation_Root_CA.pem | `1C:B9:82:3B:A2:8B:A6:AD:2D:33:A0:06:94:1D:E2:AE:4F:51:3E:F1:D4:E8:31:B9:F7:E0:FA:7B:62:42:C9:32` |
| `google_key_attestation_ca1_ecdsa.pem` | https://developer.android.com/privacy-and-security/security-key-attestation | `6D:9D:B4:CE:6C:5C:0B:29:31:66:D0:89:86:E0:57:74:A8:77:6C:EB:52:5D:9E:43:29:52:0D:E1:2B:A4:BC:C0` |
| `google_hardware_attestation_rsa_2042.pem` | same | `CE:DB:1C:B6:DC:89:6A:E5:EC:79:73:48:BC:E9:28:67:53:C2:B3:8E:E7:1C:E0:FB:E3:4A:9A:12:48:80:0D:FC` |
| `google_hardware_attestation_rsa_2034.pem` | same | `1E:F1:A0:4B:8B:A5:8A:B9:45:89:AC:49:8C:89:82:A7:83:F2:4E:A7:30:7E:01:59:A0:C3:A7:3B:37:7D:87:CC` |
| `google_hardware_attestation_rsa_2036.pem` | same | `AB:66:41:17:8A:36:E1:79:AA:0C:1C:DD:DF:9A:16:EB:45:FA:20:94:3E:2B:8C:D7:C7:C0:5C:26:CF:8B:48:7A` |

The Google RSA root that expired on 2026-05-24 is left out.
