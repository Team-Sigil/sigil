import uvicorn

from gateway.main import create_app

if __name__ == "__main__":
    uvicorn.run(create_app(), host="0.0.0.0", port=8000, log_config=None)  # noqa: S104 - in a container
