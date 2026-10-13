"""Cliente gRPC de authentication-system (Unergy Auth).

`auth.proto` viene de la skill `add-auth` (gitlab unergy-dev/dev-tools/
auth-integration-skill). `auth_pb2.py` y `auth_pb2_grpc.py` son generados: no se
editan a mano, salvo el import relativo de `auth_pb2_grpc.py`. Para regenerarlos
tras un cambio del proto:

    uv run python -m grpc_tools.protoc -I apps/comun/integraciones/unergy_auth \\
        --python_out=apps/comun/integraciones/unergy_auth \\
        --grpc_python_out=apps/comun/integraciones/unergy_auth \\
        apps/comun/integraciones/unergy_auth/auth.proto

y volver a cambiar `import auth_pb2` por `from . import auth_pb2` en
`auth_pb2_grpc.py`: protoc lo genera absoluto y aquí vive dentro de un paquete.
"""
