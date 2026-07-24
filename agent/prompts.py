from pathlib import Path
from loguru import logger
import yaml


def load_prompt(file_path):
    with open(file_path, 'r', encoding="utf-8") as f:
        return yaml.safe_load(f)

# 加载配置文件的地址
root_path = Path(__file__).parents[1]
prompt_file_path = root_path /"prompt"/"prompts.yaml"
# 提取智能体的内容
prompt_content = load_prompt(prompt_file_path)
logger.info(f"prompt_content: {prompt_content}")

main_agent_config = prompt_content["main_agent"]
logger.info(f"main_agent_config: {main_agent_config}")

sub_agents_config = prompt_content["sub_agents"]
logger.info(f"sub_agents_config: {sub_agents_config}")
