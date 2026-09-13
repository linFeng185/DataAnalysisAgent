-- 为已保存的报表能力覆盖补齐三个子页键。
--
-- 背景：report_user_permissions.capabilities 是「整体替换」语义——一旦租户管理员
-- 在管理工作区保存过某用户的报表额度，该行就取代角色默认值。而子页键
-- report.create_page / report.list / report.link_manage 是后加的，旧清单里没有它们，
-- 于是这些用户点开「报表工作台」父菜单时三个子项全被前端过滤掉，看起来像菜单坏了。
-- 该行本身是持久化数据，退出重新登录与重启进程都不会复原，因此必须回填。
--
-- 回填口径与 src/reporting/quota.py 的 _ROLE_CAPABILITIES 一致：
--   tenant_admin / analyst → 三个子页键
--   viewer                 → 只有 report.list（报表信息页）
-- 只追加缺失的键，不删除任何既有授权；用 `? 键名` 判重保证可重复执行。

UPDATE report_user_permissions AS p
SET capabilities = p.capabilities || COALESCE(
        (
            SELECT jsonb_agg(k)
            FROM (VALUES ('report.create_page'), ('report.list'), ('report.link_manage')) AS t(k)
            WHERE NOT p.capabilities ? k
        ),
        '[]'::jsonb
    )
FROM users AS u
WHERE u.id = p.user_id
  AND u.tenant_id = p.tenant_id
  AND u.role IN ('tenant_admin', 'analyst')
  AND NOT (p.capabilities ?& ARRAY['report.create_page', 'report.list', 'report.link_manage']);

UPDATE report_user_permissions AS p
SET capabilities = p.capabilities || '["report.list"]'::jsonb
FROM users AS u
WHERE u.id = p.user_id
  AND u.tenant_id = p.tenant_id
  AND u.role NOT IN ('tenant_admin', 'analyst')
  AND NOT (p.capabilities ? 'report.list');
