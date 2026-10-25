import logging
from pathlib import Path

try:
    from typing import Annotated
except ImportError:
    from typing import Annotated

from langchain_core.tools import tool

from agent.runtime.evidence_protocol import evidence_tool
from api.context import get_session_context
from api.monitor import monitor
from utils.path_utils import resolve_path
from utils.word_converter import convert_md_to_pdf_via_word


@evidence_tool("control.v1")
@tool
def convert_md_to_pdf(
    md_filename: Annotated[str, "要转换的Markdown文档路径（包含.md后缀）"],
    pdf_filename: Annotated[str | None, "输出的PDF文件路径（可选，默认与源文件同名）"] = None,
) -> str:
    """
    将Markdown文档转换为PDF（基于Word引擎）；服务模式立即返回持久化 task_id，由页面更新结果，不代表转换已完成。
    核心优化：路径与资源管理逻辑分离，只保留Tool层的基础调用
    """
    monitor.report_tool("Markdown转PDF工具")

    try:
        # 1. 路径预处理
        session_dir = get_session_context()
        md_path = Path(md_filename).with_suffix(".md")
        md_abs_path = Path(resolve_path(str(md_path), session_dir))

        # 2. 检查源文件
        if not md_abs_path.exists():
            raise FileNotFoundError("Source Markdown file does not exist")

        # 3. 确定输出路径
        if pdf_filename:
            pdf_path = Path(pdf_filename).with_suffix(".pdf")
            pdf_abs_path = Path(resolve_path(str(pdf_path), session_dir, write=True))
        else:
            pdf_abs_path = Path(resolve_path(str(md_abs_path.with_suffix(".pdf")), session_dir, write=True))

        # Long Word conversions use the durable protocol in the service.
        from agent.runtime.context import get_run_context

        context = get_run_context()
        if context and context.job_service:
            from agent.runtime.artifact_jobs import submit_conversion_sync

            return submit_conversion_sync(context, md_abs_path, pdf_abs_path)
        # Standalone tools retain their synchronous compatibility path.
        return convert_md_to_pdf_via_word(md_abs_path, pdf_abs_path)

    except Exception as e:
        logging.error(f"转换失败: {e}", exc_info=True)
        raise


if __name__ == "__main__":
    # 测试代码
    # 强制覆盖当前模块中的 get_session_context
    def get_session_context():
        return "./test_session_123"

    # 创建测试文件
    Path("./test_session_123/sub_dir").mkdir(parents=True, exist_ok=True)
    with open("./test_session_123/sub_dir/测试文件.md", "w", encoding="utf-8") as f:
        f.write("# 标题\n\n测试内容\n\n|A|B|\n|---|---|\n|1|2|")

    print(convert_md_to_pdf.invoke({"md_filename": "sub_dir/测试文件.md"}))
