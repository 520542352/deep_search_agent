import os
from dotenv import load_dotenv
from typing import Tuple, Optional

def _load_ragflow_env() ->Tuple[Optional[str], Optional[str]]:
    # 加载rag环境配置
    current_dir = os.path.dirname(os.path.abspath(__file__))
    env_path = os.path.join(current_dir, ".env")
    if os.path.exists(env_path):
        load_dotenv(env_path)
    else:
        load_dotenv()

    api_key = os.getenv("RAGFLOW_API_KEY")
    base_url = os.getenv("RAGFLOW_API_URL")
    return api_key, base_url
