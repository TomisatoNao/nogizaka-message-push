"""
tests/test_config_architecture.py — 架构升级契约测试：配置与代码物理解耦校验
"""
from __future__ import annotations

from pathlib import Path


def test_config_directory_contains_zero_python_source_files():
    """验证根目录 config/ 已经转变为 100% 纯配置数据目录，严禁包含任何 .py 源码文件。"""
    project_root = Path(__file__).resolve().parent.parent
    config_dir = project_root / "config"

    assert config_dir.is_dir(), "根目录 config/ 必须存在"
    py_files = list(config_dir.glob("*.py"))
    assert len(py_files) == 0, f"config/ 目录下不得存在任何 Python 源码文件，但发现了: {py_files}"

    # 验证关键数据与模板文件依然完好保留
    assert (config_dir / "config.example.json").is_file(), "config.example.json 必须保留"
    assert (config_dir / "config.schema.json").is_file(), "config.schema.json 必须保留"


def test_src_config_package_structure_and_exports():
    """验证 src/config/ 源码包完整性与其导出符号。"""
    project_root = Path(__file__).resolve().parent.parent
    src_config_dir = project_root / "src" / "config"

    assert src_config_dir.is_dir(), "src/config/ 必须存在"
    assert (src_config_dir / "__init__.py").is_file()
    assert (src_config_dir / "config.py").is_file()
    assert (src_config_dir / "credentials.py").is_file()
    assert (src_config_dir / "watcher.py").is_file()

    import src.config as sc
    assert hasattr(sc, "cfg")
    assert hasattr(sc, "credentials")
    assert hasattr(sc, "watcher")


def test_memory_singleton_alias_compatibility():
    """验证 sys.modules 内存单例别名注入，确保存量或外部代码使用旧路径时完全指向同一对象。"""
    import src.config.config as sc_cfg
    import config.config as c_cfg
    assert sc_cfg is c_cfg, "src.config.config 与 config.config 必须在 sys.modules 中指向同一单例"

    import src.config.credentials as sc_cred
    import config.credentials as c_cred
    assert sc_cred is c_cred, "src.config.credentials 与 config.credentials 必须在 sys.modules 中指向同一单例"


def test_path_resolution_hierarchy():
    """验证路径推导在 src/config 下能够准确回溯至项目根目录，并准确识别 config.json。"""
    from src.config import config as cfg

    project_root = Path(__file__).resolve().parent.parent
    assert cfg._BASE_DIR.resolve() == project_root.resolve(), f"基准目录推导错误: {cfg._BASE_DIR} != {project_root}"
    assert cfg._CONFIG_PATH.resolve() == (project_root / "config" / "config.json").resolve()
    assert cfg._CONFIG_PATH.exists() is True, "必须正确定位到现有的 config.json 配置文件"
    assert cfg._EXAMPLE_PATH.resolve() == (project_root / "config" / "config.example.json").resolve()
    assert cfg._SCHEMA_PATH.resolve() == (project_root / "config" / "config.schema.json").resolve()


def test_config_reload_in_place_mutation():
    """验证热重载 reload() 在新架构下依然具备容器原地更新能力。"""
    from src.config import config as cfg

    original_monitor = cfg.MONITOR_LIST
    assert isinstance(original_monitor, list)

    ok = cfg.reload()
    assert ok is True
    # 原地更新验证：热重载后对象内存地址一致（容器原地变动）
    assert cfg.MONITOR_LIST is original_monitor
