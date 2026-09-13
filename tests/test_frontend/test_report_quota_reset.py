"""报表额度「本周期已用量重置」前端契约测试，覆盖 spec/48 第 5 节需求 4.1-4.4。"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

CATALOGUE = "frontend/src/components/report/quotaResetFields.ts"
EDITOR = "frontend/src/components/report/ReportPolicyEditor.tsx"
ADMIN = "frontend/src/pages/AdminPage.tsx"
API = "frontend/src/api/reportWorkbench.ts"
TYPES = "frontend/src/types/report.ts"

# 冻结契约：可重置的四个按月字段及其界面中文名，任何一处改动都必须先改后端契约。
FIELD_NAMES = (
    ("jobs_created", "每月可创建任务数"),
    ("runs_started", "每月可运行次数"),
    ("llm_tokens", "每月 Token 预算"),
    ("share_links", "每月分享链接上限"),
)


class TestQuotaResetFieldCatalogue:
    """覆盖需求 4.2：四个可重置字段的清单只有一个定义来源。"""

    # 方法作用：验证字段清单只在共享模块里声明一次，弹窗与页面都引用同一份。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_reset_fields_have_single_definition_source(self) -> None:
        """字段键与中文名重复声明会导致两个入口的选项漂移，必须只留一份。"""
        logger.debug("test_reset_fields_have_single_definition_source 入口")
        # Arrange
        catalogue = Path(CATALOGUE).read_text(encoding="utf-8")
        editor = Path(EDITOR).read_text(encoding="utf-8")
        admin = Path(ADMIN).read_text(encoding="utf-8")

        # Act / Assert
        for key, label in FIELD_NAMES:
            # 共享模块声明字段键与延迟求值的中文标签
            assert f"value: '{key}'" in catalogue
            assert f'return t("{label}")' in catalogue
            # 消费方不得再写一份字段键，否则字段清单就有两个来源
            for source in (editor, admin):
                assert f"'{key}'" not in source
        # 两个消费方都从同一个模块导入选项常量
        assert "QUOTA_RESET_FIELDS" in editor and "from './quotaResetFields'" in editor
        assert "QUOTA_RESET_FIELDS" in admin and "from '../components/report/quotaResetFields'" in admin
        logger.info("test_reset_fields_have_single_definition_source 完成")

    # 方法作用：验证两个入口的多选控件都直接消费共享常量而不是各自拼装选项。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_both_entries_consume_shared_options_constant(self) -> None:
        """弹窗与页面都必须把共享常量交给 Checkbox.Group 的 options。"""
        logger.debug("test_both_entries_consume_shared_options_constant 入口")
        # Arrange
        editor = Path(EDITOR).read_text(encoding="utf-8")
        admin = Path(ADMIN).read_text(encoding="utf-8")

        # Act / Assert
        for source in (editor, admin):
            assert "Checkbox.Group" in source
            assert "options={REPORT_QUOTA_RESET_FIELDS}" in source
        # 编辑弹窗的重置区在额度表单之外，避免被「保存额度」一起提交
        assert editor.index("options={REPORT_QUOTA_RESET_FIELDS}") > editor.index("</Form>")
        logger.info("test_both_entries_consume_shared_options_constant 完成")


class TestReportPolicyEditorReset:
    """覆盖需求 4.1、4.3：单用户重置的交互与禁用条件。"""

    # 方法作用：验证未勾选字段时重置按钮与二次确认都处于禁用态。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_editor_reset_button_disabled_without_fields(self) -> None:
        """空字段列表会被后端 422 拒绝，前端必须先禁用。"""
        logger.debug("test_editor_reset_button_disabled_without_fields 入口")
        # Arrange
        editor = Path(EDITOR).read_text(encoding="utf-8")

        # Act / Assert
        assert "resetFields.length === 0" in editor
        # 按钮与 Popconfirm 各自都要禁用，否则二次确认仍会弹出
        assert editor.count("disabled={resetFields.length === 0}") >= 2
        assert 't("重置已用量")' in editor
        logger.info("test_editor_reset_button_disabled_without_fields 完成")

    # 方法作用：验证重置前有不可撤销的二次确认，且成功后不关闭弹窗。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_editor_reset_confirms_irreversible_and_keeps_modal_open(self) -> None:
        """重置不可撤销必须写进确认文案；成功后保留弹窗便于接着改上限。"""
        logger.debug("test_editor_reset_confirms_irreversible_and_keeps_modal_open 入口")
        # Arrange
        editor = Path(EDITOR).read_text(encoding="utf-8")
        reset_body = editor[editor.index("const resetUsage"):editor.index("return <Modal")]

        # Act / Assert
        assert "Popconfirm" in editor
        assert 't("将清零选中字段的本月已用量，且不可撤销。")' in editor
        # 顶部说明必须同时讲清「保存只改上限」和「重置只清用量」，不能自相矛盾
        assert "保存额度只修改上限、不清空已用量" in editor
        assert "重置已用量只清零本周期用量、不修改上限" in editor
        assert 't("清零选中字段在当前自然月的已用量，不修改上面的额度上限；重置结果不可撤销。")' in editor
        # 重置成功不关闭弹窗：整个处理函数不得调用 onClose
        assert "onClose()" not in reset_body
        assert "resetReportQuotaUsage" in reset_body
        logger.info("test_editor_reset_confirms_irreversible_and_keeps_modal_open 完成")

    # 方法作用：验证单用户重置只针对当前用户，并按后端返回的实际人数提示。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_editor_reset_targets_current_user_and_reports_actual_count(self) -> None:
        """提示必须来自 quotaResetResultMessage（内含 reset_users），不得硬编码个数。"""
        logger.debug("test_editor_reset_targets_current_user_and_reports_actual_count 入口")
        # Arrange
        editor = Path(EDITOR).read_text(encoding="utf-8")
        catalogue = Path(CATALOGUE).read_text(encoding="utf-8")

        # Act / Assert
        assert "resetReportQuotaUsage({ user_ids: [user.id], fields: resetFields })" in editor
        assert "message.success(quotaResetResultMessage(result, 1))" in editor
        # 实际清零人数只能来自响应字段，不能写死
        assert "reset_users" not in editor
        assert "value0: response.reset_users" in catalogue
        logger.info("test_editor_reset_targets_current_user_and_reports_actual_count 完成")

    # 方法作用：验证实际清零人数少于请求人数时提示会说明差异。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_result_message_explains_fewer_reset_users(self) -> None:
        """reset_users 可能小于请求数，提示必须如实反映而不是报请求数。"""
        logger.debug("test_result_message_explains_fewer_reset_users 入口")
        # Arrange
        catalogue = Path(CATALOGUE).read_text(encoding="utf-8")

        # Act / Assert
        assert "if (response.reset_users >= requestedCount) return summary;" in catalogue
        assert "另有 {value1} 个用户本期没有已用量，未发生变更。" in catalogue
        assert "quotaResetFieldLabels(response.fields)" in catalogue
        logger.info("test_result_message_explains_fewer_reset_users 完成")


class TestAdminBatchQuotaReset:
    """覆盖需求 4.3、4.4：租户管理员的多用户批量与全租户重置。"""

    # 方法作用：验证只有本租户用户表格启用行多选，租户表格不受影响。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_row_selection_only_on_tenant_user_table(self) -> None:
        """行多选若挂到租户表格会把「删除租户」等操作也带上多选语义。"""
        logger.debug("test_row_selection_only_on_tenant_user_table 入口")
        # Arrange
        admin = Path(ADMIN).read_text(encoding="utf-8")
        tenant_workspace = admin[admin.index("const tenantWorkspace"):admin.index("const userWorkspace")]

        # Act / Assert
        assert admin.count("rowSelection") == 1
        assert "rowSelection={isTenantAdmin ? {" in admin
        assert "selectedRowKeys: selectedUserIds" in admin
        assert "rowSelection" not in tenant_workspace
        logger.info("test_row_selection_only_on_tenant_user_table 完成")

    # 方法作用：验证批量入口按钮与「报表额度」按钮同为租户管理员专属。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_batch_button_shares_tenant_admin_guard(self) -> None:
        """权限判断必须与既有「报表额度」按钮一致，不得另立规则。"""
        logger.debug("test_batch_button_shares_tenant_admin_guard 入口")
        # Arrange
        admin = Path(ADMIN).read_text(encoding="utf-8")

        # Act / Assert
        assert '{isTenantAdmin && <Button onClick={openBatchReset}>{t("批量重置报表额度")}</Button>}' in admin
        assert '{isTenantAdmin && <Button onClick={() => setReportPolicyUser(user)}>{t("报表额度")}</Button>}' in admin
        assert 't("批量重置报表额度")' in admin
        logger.info("test_batch_button_shares_tenant_admin_guard 完成")

    # 方法作用：验证批量弹窗提供「已选中的 N 个用户」与「当前租户全部用户」两条范围路径。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_batch_modal_supports_selected_and_all_users_paths(self) -> None:
        """范围选择必须能映射到 all_users 或 user_ids 两条互斥请求路径。"""
        logger.debug("test_batch_modal_supports_selected_and_all_users_paths 入口")
        # Arrange
        admin = Path(ADMIN).read_text(encoding="utf-8")

        # Act / Assert
        assert "Radio.Group" in admin
        assert "batchResetScope" in admin
        assert '<Radio value="selected">{t("已选中的 {value0} 个用户"' in admin
        assert '<Radio value="all">{t("当前租户全部用户")}</Radio>' in admin
        # 全租户只发 all_users，指定用户只发 user_ids，两者互斥
        assert "await resetReportQuotaUsage({ all_users: true, fields: batchResetFields })" in admin
        assert "await resetReportQuotaUsage({ user_ids: targetUserIds, fields: batchResetFields })" in admin
        logger.info("test_batch_modal_supports_selected_and_all_users_paths 完成")

    # 方法作用：验证缺少目标用户或缺字段时批量提交被禁用，确认后按实际人数提示。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_batch_submit_disabled_and_reports_actual_count(self) -> None:
        """没有目标或没有字段时不允许提交，成功后展示实际清零人数与实际字段。"""
        logger.debug("test_batch_submit_disabled_and_reports_actual_count 入口")
        # Arrange
        admin = Path(ADMIN).read_text(encoding="utf-8")

        # Act / Assert
        assert "const batchResetDisabled = batchResetFields.length === 0" in admin
        assert "(batchResetScope === 'selected' && selectedUserIds.length === 0);" in admin
        assert "disabled={batchResetDisabled}" in admin
        assert "Popconfirm" in admin
        assert "onConfirm={() => void submitBatchReset()}" in admin
        assert 't("将清零选中字段的本月已用量，且不可撤销。")' in admin
        # 提示由共享方法生成：内部使用服务端 reset_users，而不是请求数
        assert "message.success(quotaResetResultMessage(result, requestedCount));" in admin
        assert "const requestedCount = scopeAllUsers ? users.length : targetUserIds.length;" in admin
        logger.info("test_batch_submit_disabled_and_reports_actual_count 完成")


class TestQuotaResetApiContract:
    """覆盖需求 4.4：请求体字段名与后端冻结契约一致。"""

    # 方法作用：验证 API 封装走冻结端点，并按范围互斥地提交 user_ids 或 all_users。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_api_posts_frozen_body_to_frozen_endpoint(self) -> None:
        """请求体字段名写错会让后端收到空范围并返回 422。"""
        logger.debug("test_api_posts_frozen_body_to_frozen_endpoint 入口")
        # Arrange
        api = Path(API).read_text(encoding="utf-8")
        types = Path(TYPES).read_text(encoding="utf-8")

        # Act / Assert
        assert (
            "export function resetReportQuotaUsage(request: ReportQuotaUsageResetRequest)"
            ": Promise<ReportQuotaUsageResetResponse>" in api
        )
        assert "`${REPORT_BASE}/admin/quota-usage/reset`" in api
        assert "const body: Record<string, unknown> = { fields: request.fields };" in api
        assert "if (request.all_users) body.all_users = true;" in api
        assert "else body.user_ids = request.user_ids || [];" in api
        # 类型契约与后端响应字段一一对应
        assert "user_ids?: number[];" in types
        assert "all_users?: boolean;" in types
        assert "fields: ReportQuotaResetField[];" in types
        assert "reset_users: number;" in types
        assert "period_start: string;" in types and "period_end: string;" in types
        logger.info("test_api_posts_frozen_body_to_frozen_endpoint 完成")

    # 方法作用：验证字段键的联合类型与四个可重置字段完全一致。
    # Args: self - pytest 测试类实例。
    # Returns: 无返回值，断言失败时由 pytest 报告。
    def test_field_union_matches_four_resettable_keys(self) -> None:
        """联合类型多一个键会让界面出现后端不认的选项。"""
        logger.debug("test_field_union_matches_four_resettable_keys 入口")
        # Arrange
        types = Path(TYPES).read_text(encoding="utf-8")

        # Act / Assert
        assert (
            "export type ReportQuotaResetField = "
            "'jobs_created' | 'runs_started' | 'llm_tokens' | 'share_links';" in types
        )
        logger.info("test_field_union_matches_four_resettable_keys 完成")
