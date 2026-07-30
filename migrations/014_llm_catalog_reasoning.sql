-- 平台模型物理删除、厂商动态能力表单与对话推理参数治理。

ALTER TABLE llm_provider_catalog
    ADD COLUMN IF NOT EXISTS capability_schema JSONB NOT NULL DEFAULT '{"fields":[]}'::jsonb;

UPDATE llm_provider_catalog
SET capability_schema = '{"fields":[
  {"key":"streaming","label":"流式输出","type":"boolean","required":false,"default":true,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"reasoning","label":"支持推理","type":"boolean","required":false,"default":false,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"reasoning_content_in_response","label":"返回推理字段","type":"boolean","required":false,"default":false,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"function_calling","label":"工具调用","type":"boolean","required":false,"default":true,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"json_mode","label":"JSON 输出","type":"boolean","required":false,"default":true,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"vision","label":"视觉输入","type":"boolean","required":false,"default":false,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"context_window","label":"上下文窗口","type":"integer","required":true,"default":128000,"options":[],"minimum":1,"maximum":null,"description":""},
  {"key":"max_tokens_limit","label":"最大输出 Token","type":"integer","required":true,"default":8192,"options":[],"minimum":1,"maximum":null,"description":""},
  {"key":"reasoning_efforts","label":"支持的推理深度","type":"multiselect","required":false,"default":[],"options":[{"label":"低","value":"low"},{"label":"中","value":"medium"},{"label":"高","value":"high"},{"label":"最大","value":"max"}],"minimum":null,"maximum":null,"description":""},
  {"key":"reasoning_default_effort","label":"默认推理深度","type":"select","required":false,"default":"high","options":[{"label":"低","value":"low"},{"label":"中","value":"medium"},{"label":"高","value":"high"},{"label":"最大","value":"max"}],"minimum":null,"maximum":null,"description":""},
  {"key":"reasoning_default_enabled","label":"对话默认开启推理","type":"boolean","required":false,"default":false,"options":[],"minimum":null,"maximum":null,"description":""}
]}'::jsonb
WHERE capability_schema = '{}'::jsonb OR capability_schema = '{"fields":[]}'::jsonb;

UPDATE llm_provider_catalog
SET default_base_url = 'https://api.deepseek.com',
    capability_schema = '{"fields":[
  {"key":"streaming","label":"流式输出","type":"boolean","required":false,"default":true,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"reasoning","label":"支持推理","type":"boolean","required":false,"default":false,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"reasoning_content_in_response","label":"返回推理字段","type":"boolean","required":false,"default":false,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"function_calling","label":"工具调用","type":"boolean","required":false,"default":true,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"json_mode","label":"JSON 输出","type":"boolean","required":false,"default":true,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"vision","label":"视觉输入","type":"boolean","required":false,"default":false,"options":[],"minimum":null,"maximum":null,"description":""},
  {"key":"context_window","label":"上下文窗口","type":"integer","required":true,"default":128000,"options":[],"minimum":1,"maximum":null,"description":""},
  {"key":"max_tokens_limit","label":"最大输出 Token","type":"integer","required":true,"default":8192,"options":[],"minimum":1,"maximum":null,"description":""},
  {"key":"reasoning_efforts","label":"支持的推理深度","type":"multiselect","required":false,"default":[],"options":[{"label":"高","value":"high"},{"label":"最大","value":"max"}],"minimum":null,"maximum":null,"description":""},
  {"key":"reasoning_default_effort","label":"默认推理深度","type":"select","required":false,"default":"high","options":[{"label":"高","value":"high"},{"label":"最大","value":"max"}],"minimum":null,"maximum":null,"description":""},
  {"key":"reasoning_default_enabled","label":"对话默认开启推理","type":"boolean","required":false,"default":true,"options":[],"minimum":null,"maximum":null,"description":""}
]}'::jsonb,
    updated_at = NOW()
WHERE LOWER(code) = 'deepseek';

INSERT INTO llm_model_catalog (provider_id, model_id, display_name, capabilities)
SELECT id, 'deepseek-v4-pro', 'DeepSeek V4 Pro',
       '{"streaming":true,"reasoning":true,"reasoning_content_in_response":true,"function_calling":true,"json_mode":true,"vision":false,"context_window":1000000,"max_tokens_limit":8192,"reasoning_efforts":["high","max"],"reasoning_default_effort":"high","reasoning_default_enabled":true}'::jsonb
FROM llm_provider_catalog WHERE LOWER(code) = 'deepseek'
ON CONFLICT (provider_id, model_id) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    capabilities = EXCLUDED.capabilities,
    is_active = TRUE,
    updated_at = NOW();

INSERT INTO llm_model_catalog (provider_id, model_id, display_name, capabilities)
SELECT id, 'deepseek-v4-flash', 'DeepSeek V4 Flash',
       '{"streaming":true,"reasoning":true,"reasoning_content_in_response":true,"function_calling":true,"json_mode":true,"vision":false,"context_window":1000000,"max_tokens_limit":8192,"reasoning_efforts":["high","max"],"reasoning_default_effort":"high","reasoning_default_enabled":true}'::jsonb
FROM llm_provider_catalog WHERE LOWER(code) = 'deepseek'
ON CONFLICT (provider_id, model_id) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    capabilities = EXCLUDED.capabilities,
    is_active = TRUE,
    updated_at = NOW();

DO $$
DECLARE
    old_model_id BIGINT;
    pro_model_id BIGINT;
BEGIN
    SELECT old_model.id INTO old_model_id
    FROM llm_model_catalog old_model
    JOIN llm_provider_catalog provider ON provider.id = old_model.provider_id
    WHERE LOWER(provider.code) = 'deepseek' AND old_model.model_id = 'deepseek-chat';

    SELECT current_model.id INTO pro_model_id
    FROM llm_model_catalog current_model
    JOIN llm_provider_catalog provider ON provider.id = current_model.provider_id
    WHERE LOWER(provider.code) = 'deepseek' AND current_model.model_id = 'deepseek-v4-pro';

    IF old_model_id IS NOT NULL AND pro_model_id IS NOT NULL THEN
        INSERT INTO tenant_llm_connection_models (connection_id, model_catalog_id, is_enabled)
        SELECT connection_id, pro_model_id, is_enabled
        FROM tenant_llm_connection_models
        WHERE model_catalog_id = old_model_id
        ON CONFLICT (connection_id, model_catalog_id) DO UPDATE
            SET is_enabled = EXCLUDED.is_enabled;

        UPDATE tenant_llm_defaults
        SET model_catalog_id = pro_model_id, updated_at = NOW()
        WHERE model_catalog_id = old_model_id;

        DELETE FROM tenant_llm_connection_models WHERE model_catalog_id = old_model_id;
        DELETE FROM llm_model_catalog WHERE id = old_model_id;
    END IF;
END;
$$;
