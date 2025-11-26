from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    subtensor_endpoint: str
    """Endpoint of the Subtensor network"""
    subtensor_fallback: str | None = None
    """Fallback endpoint of the Subtensor network"""
    netuid: int
    """Netuid of the Subtensor network"""

    wallet_name: str
    """Name of the wallet to use for the CLI"""
    wallet_hotkey: str
    """Hotkey of the wallet to use for the CLI"""

    class Config:
        env_file = ".env"
        env_file_encoding = "utf-8"


settings = Settings()  # type: ignore
