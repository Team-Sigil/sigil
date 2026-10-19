import uvicorn

from gateway.main import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8000)  # noqa: S104 - runs in a container
