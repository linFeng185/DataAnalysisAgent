"""主动洞察与定时报告自动化模块。"""

from src.automation.models import InsightEvent, ScheduleDefinition
from src.automation.runner import ScheduledAnalysisRunner
from src.automation.service import AutomationService

__all__ = ["AutomationService", "InsightEvent", "ScheduleDefinition", "ScheduledAnalysisRunner"]
