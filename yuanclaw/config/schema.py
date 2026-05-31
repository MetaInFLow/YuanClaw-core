"""Configuration schema using Pydantic."""

from pathlib import Path
from typing import Any, Literal

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from pydantic_settings import BaseSettings


class Base(BaseModel):
    """Base model that accepts both camelCase and snake_case keys."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class ChannelBase(Base):
    """Base model for channel sections that may carry plugin-specific extras."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")


class WhatsAppConfig(ChannelBase):
    """WhatsApp channel configuration."""

    enabled: bool = False
    bridge_url: str = "ws://localhost:3001"
    bridge_token: str = ""  # Shared token for bridge auth (optional, recommended)
    allow_from: list[str] = Field(default_factory=list)  # Allowed phone numbers


class TelegramConfig(ChannelBase):
    """Telegram channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from @BotFather
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs or usernames
    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    reply_to_message: bool = False  # If true, bot replies quote the original message
    group_policy: Literal["open", "mention"] = "mention"  # "mention" responds when @mentioned or replied to, "open" responds to all


class FeishuConfig(ChannelBase):
    """Feishu/Lark channel configuration using WebSocket long connection."""

    enabled: bool = False
    app_id: str = ""  # App ID from Feishu Open Platform
    app_secret: str = ""  # App Secret from Feishu Open Platform
    encrypt_key: str = ""  # Encrypt Key for event subscription (optional)
    verification_token: str = ""  # Verification Token for event subscription (optional)
    allow_from: list[str] = Field(default_factory=list)  # Allowed user open_ids
    react_emoji: str = (
        "THUMBSUP"  # Emoji type for message reactions (e.g. THUMBSUP, OK, DONE, SMILE)
    )


class DingTalkConfig(ChannelBase):
    """DingTalk channel configuration using Stream mode."""

    enabled: bool = False
    client_id: str = ""  # AppKey
    client_secret: str = ""  # AppSecret
    allow_from: list[str] = Field(default_factory=list)  # Allowed staff_ids


class DiscordConfig(ChannelBase):
    """Discord channel configuration."""

    enabled: bool = False
    token: str = ""  # Bot token from Discord Developer Portal
    allow_from: list[str] = Field(default_factory=list)  # Allowed user IDs
    gateway_url: str = "wss://gateway.discord.gg/?v=10&encoding=json"
    intents: int = 37377  # GUILDS + GUILD_MESSAGES + DIRECT_MESSAGES + MESSAGE_CONTENT
    group_policy: Literal["mention", "open"] = "mention"


class MatrixConfig(ChannelBase):
    """Matrix (Element) channel configuration."""

    enabled: bool = False
    homeserver: str = "https://matrix.org"
    access_token: str = ""
    user_id: str = ""  # @bot:matrix.org
    device_id: str = ""
    e2ee_enabled: bool = True  # Enable Matrix E2EE support (encryption + encrypted room handling).
    sync_stop_grace_seconds: int = (
        2  # Max seconds to wait for sync_forever to stop gracefully before cancellation fallback.
    )
    max_media_bytes: int = (
        20 * 1024 * 1024
    )  # Max attachment size accepted for Matrix media handling (inbound + outbound).
    allow_from: list[str] = Field(default_factory=list)
    group_policy: Literal["open", "mention", "allowlist"] = "open"
    group_allow_from: list[str] = Field(default_factory=list)
    allow_room_mentions: bool = False


class EmailConfig(ChannelBase):
    """Email channel configuration (IMAP inbound + SMTP outbound)."""

    enabled: bool = False
    consent_granted: bool = False  # Explicit owner permission to access mailbox data

    # IMAP (receive)
    imap_host: str = ""
    imap_port: int = 993
    imap_username: str = ""
    imap_password: str = ""
    imap_mailbox: str = "INBOX"
    imap_use_ssl: bool = True

    # SMTP (send)
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_username: str = ""
    smtp_password: str = ""
    smtp_use_tls: bool = True
    smtp_use_ssl: bool = False
    from_address: str = ""

    # Behavior
    auto_reply_enabled: bool = (
        True  # If false, inbound email is read but no automatic reply is sent
    )
    poll_interval_seconds: int = 30
    mark_seen: bool = True
    max_body_chars: int = 12000
    subject_prefix: str = "Re: "
    allow_from: list[str] = Field(default_factory=list)  # Allowed sender email addresses


class MochatMentionConfig(ChannelBase):
    """Mochat mention behavior configuration."""

    require_in_groups: bool = False


class MochatGroupRule(ChannelBase):
    """Mochat per-group mention requirement."""

    require_mention: bool = False


class MochatConfig(ChannelBase):
    """Mochat channel configuration."""

    enabled: bool = False
    base_url: str = "https://mochat.io"
    socket_url: str = ""
    socket_path: str = "/socket.io"
    socket_disable_msgpack: bool = False
    socket_reconnect_delay_ms: int = 1000
    socket_max_reconnect_delay_ms: int = 10000
    socket_connect_timeout_ms: int = 10000
    refresh_interval_ms: int = 30000
    watch_timeout_ms: int = 25000
    watch_limit: int = 100
    retry_delay_ms: int = 500
    max_retry_attempts: int = 0  # 0 means unlimited retries
    claw_token: str = ""
    agent_user_id: str = ""
    sessions: list[str] = Field(default_factory=list)
    panels: list[str] = Field(default_factory=list)
    allow_from: list[str] = Field(default_factory=list)
    mention: MochatMentionConfig = Field(default_factory=MochatMentionConfig)
    groups: dict[str, MochatGroupRule] = Field(default_factory=dict)
    reply_delay_mode: str = "non-mention"  # off | non-mention
    reply_delay_ms: int = 120000


class SlackDMConfig(ChannelBase):
    """Slack DM policy configuration."""

    enabled: bool = True
    policy: str = "open"  # "open" or "allowlist"
    allow_from: list[str] = Field(default_factory=list)  # Allowed Slack user IDs


class SlackConfig(ChannelBase):
    """Slack channel configuration."""

    enabled: bool = False
    mode: str = "socket"  # "socket" supported
    webhook_path: str = "/slack/events"
    bot_token: str = ""  # xoxb-...
    app_token: str = ""  # xapp-...
    user_token_read_only: bool = True
    reply_in_thread: bool = True
    react_emoji: str = "eyes"
    allow_from: list[str] = Field(default_factory=list)  # Allowed Slack user IDs (sender-level)
    group_policy: str = "mention"  # "mention", "open", "allowlist"
    group_allow_from: list[str] = Field(default_factory=list)  # Allowed channel IDs if allowlist
    dm: SlackDMConfig = Field(default_factory=SlackDMConfig)


class QQConfig(ChannelBase):
    """QQ channel configuration using botpy SDK."""

    enabled: bool = False
    app_id: str = ""  # 机器人 ID (AppID) from q.qq.com
    secret: str = ""  # 机器人密钥 (AppSecret) from q.qq.com
    allow_from: list[str] = Field(
        default_factory=list
    )  # Allowed user openids (empty = public access)


class WeComConfig(ChannelBase):
    """WeCom (Enterprise WeChat) AI bot channel configuration."""

    enabled: bool = False
    bot_id: str = ""
    secret: str = ""
    allow_from: list[str] = Field(default_factory=list)
    welcome_message: str = ""


class SignalDMConfig(Base):
    """Signal DM policy configuration."""

    enabled: bool = False
    policy: Literal["open", "allowlist"] = "allowlist"
    allow_from: list[str] = Field(default_factory=list)
    pairing_reply_enabled: bool = False
    pairing_ttl_s: int = Field(default=600, ge=30)


class SignalGroupConfig(Base):
    """Signal group policy configuration."""

    enabled: bool = False
    policy: Literal["open", "allowlist"] = "allowlist"
    allow_from: list[str] = Field(default_factory=list)
    require_mention: bool = True


class SignalConfig(ChannelBase):
    """Signal channel configuration using signal-cli daemon JSON-RPC."""

    enabled: bool = False
    phone_number: str = ""
    daemon_host: str = "localhost"
    daemon_port: int = 8080
    attachments_dir: str | None = None
    health_check_enabled: bool = True
    receive_events: bool = True
    reconnect_delay_s: float = Field(default=1.0, ge=0)
    max_reconnect_delay_s: float = Field(default=30.0, ge=0)
    group_message_buffer_size: int = Field(default=20, ge=1)
    dm: SignalDMConfig = Field(default_factory=SignalDMConfig)
    group: SignalGroupConfig = Field(default_factory=SignalGroupConfig)

    @property
    def allow_from(self) -> list[str]:
        return list(dict.fromkeys(self.dm.allow_from + self.group.allow_from))




class ChannelsConfig(Base):
    """Configuration for chat channels."""

    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="allow")

    send_progress: bool = True  # stream agent's text progress to the channel
    send_tool_hints: bool = False  # stream tool-call hints (e.g. read_file("…"))
    whatsapp: WhatsAppConfig = Field(default_factory=WhatsAppConfig)
    telegram: TelegramConfig = Field(default_factory=TelegramConfig)
    discord: DiscordConfig = Field(default_factory=DiscordConfig)
    feishu: FeishuConfig = Field(default_factory=FeishuConfig)
    mochat: MochatConfig = Field(default_factory=MochatConfig)
    dingtalk: DingTalkConfig = Field(default_factory=DingTalkConfig)
    email: EmailConfig = Field(default_factory=EmailConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)
    qq: QQConfig = Field(default_factory=QQConfig)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    wecom: WeComConfig = Field(default_factory=WeComConfig)
    signal: SignalConfig = Field(default_factory=SignalConfig)


class MemoryConfig(Base):
    """Memory backend and recall configuration."""

    backend: Literal["legacy", "core"] = "legacy"
    daily_pages: bool = True
    recent_days: int = 2
    search_max_results: int = 8
    extra_paths: list[str] = Field(default_factory=list)


class MemoryFlushConfig(Base):
    """Pre-compaction memory flush configuration."""

    enabled: bool = True
    soft_threshold_tokens: int = 4000


class CompactionConfig(Base):
    """Compaction budget and flush thresholds."""

    reserve_tokens_floor: int = 12000
    memory_flush: MemoryFlushConfig = Field(default_factory=MemoryFlushConfig)
    session_ttl_minutes: int = Field(
        default=0,
        ge=0,
        validation_alias=AliasChoices("idleCompactAfterMinutes", "sessionTtlMinutes", "session_ttl_minutes"),
        serialization_alias="idleCompactAfterMinutes",
    )


class GenerationConfig(Base):
    """Shared model generation settings."""

    model: str = "anthropic/claude-opus-4-5"
    provider: str = (
        "auto"  # Provider name (e.g. "anthropic", "openrouter") or "auto" for auto-detection
    )
    max_tokens: int = 8192
    context_window_tokens: int = 65536
    temperature: float = 0.1
    reasoning_effort: str | None = None  # low / medium / high — enables LLM thinking mode


class InlineFallbackConfig(Base):
    """Inline fallback model configuration."""

    model: str
    provider: str = "auto"
    max_tokens: int | None = None
    context_window_tokens: int | None = None
    temperature: float | None = None
    reasoning_effort: str | None = None


class ModelPresetConfig(GenerationConfig):
    """Named model preset configuration."""

    def to_generation_settings(self):
        from yuanclaw.providers.base import GenerationSettings

        return GenerationSettings(
            temperature=self.temperature,
            max_tokens=self.max_tokens,
            reasoning_effort=self.reasoning_effort,
        )


class AgentDefaults(GenerationConfig):
    """Default agent configuration."""

    workspace: str = "~/.yuanclaw/workspace"
    max_tool_iterations: int = 40
    max_concurrent_subagents: int = Field(default=1, ge=1)
    memory_window: int = 100
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    compaction: CompactionConfig = Field(default_factory=CompactionConfig)
    fallback_models: list[str | InlineFallbackConfig] = Field(default_factory=list)

    def to_model_preset(self) -> ModelPresetConfig:
        return ModelPresetConfig(
            model=self.model,
            provider=self.provider,
            max_tokens=self.max_tokens,
            context_window_tokens=self.context_window_tokens,
            temperature=self.temperature,
            reasoning_effort=self.reasoning_effort,
        )


class AgentsConfig(Base):
    """Agent configuration."""

    defaults: AgentDefaults = Field(default_factory=AgentDefaults)


class ProviderConfig(Base):
    """LLM provider configuration."""

    api_key: str = ""
    api_base: str | None = None
    extra_headers: dict[str, str] | None = None  # Custom headers (e.g. APP-Code for AiHubMix)
    extra_body: dict[str, Any] | None = None
    api_type: str = "auto"
    region: str | None = None
    profile: str | None = None


class ProvidersConfig(Base):
    """Configuration for LLM providers."""

    custom: ProviderConfig = Field(default_factory=ProviderConfig)  # Any OpenAI-compatible endpoint
    azure_openai: ProviderConfig = Field(default_factory=ProviderConfig)  # Azure OpenAI (model = deployment name)
    anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    openai: ProviderConfig = Field(default_factory=ProviderConfig)
    openrouter: ProviderConfig = Field(default_factory=ProviderConfig)
    huggingface: ProviderConfig = Field(default_factory=ProviderConfig)
    skywork: ProviderConfig = Field(default_factory=ProviderConfig)
    bedrock: ProviderConfig = Field(default_factory=ProviderConfig)
    deepseek: ProviderConfig = Field(default_factory=ProviderConfig)
    groq: ProviderConfig = Field(default_factory=ProviderConfig)
    zhipu: ProviderConfig = Field(default_factory=ProviderConfig)
    dashscope: ProviderConfig = Field(default_factory=ProviderConfig)  # 阿里云通义千问
    vllm: ProviderConfig = Field(default_factory=ProviderConfig)
    ollama: ProviderConfig = Field(default_factory=ProviderConfig)  # Ollama local models
    ovms: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenVINO Model Server
    gemini: ProviderConfig = Field(default_factory=ProviderConfig)
    moonshot: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax: ProviderConfig = Field(default_factory=ProviderConfig)
    minimax_anthropic: ProviderConfig = Field(default_factory=ProviderConfig)
    mistral: ProviderConfig = Field(default_factory=ProviderConfig)
    stepfun: ProviderConfig = Field(default_factory=ProviderConfig)
    xiaomi_mimo: ProviderConfig = Field(default_factory=ProviderConfig)
    longcat: ProviderConfig = Field(default_factory=ProviderConfig)
    ant_ling: ProviderConfig = Field(default_factory=ProviderConfig)
    aihubmix: ProviderConfig = Field(default_factory=ProviderConfig)  # AiHubMix API gateway
    siliconflow: ProviderConfig = Field(default_factory=ProviderConfig)  # SiliconFlow (硅基流动)
    novita: ProviderConfig = Field(default_factory=ProviderConfig)
    volcengine: ProviderConfig = Field(default_factory=ProviderConfig)  # VolcEngine (火山引擎)
    volcengine_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)
    byteplus: ProviderConfig = Field(default_factory=ProviderConfig)
    byteplus_coding_plan: ProviderConfig = Field(default_factory=ProviderConfig)
    lm_studio: ProviderConfig = Field(default_factory=ProviderConfig)
    atomic_chat: ProviderConfig = Field(default_factory=ProviderConfig)
    nvidia: ProviderConfig = Field(default_factory=ProviderConfig)
    qianfan: ProviderConfig = Field(default_factory=ProviderConfig)
    openai_codex: ProviderConfig = Field(default_factory=ProviderConfig)  # OpenAI Codex (OAuth)
    github_copilot: ProviderConfig = Field(default_factory=ProviderConfig)  # Github Copilot (OAuth)


class HeartbeatConfig(Base):
    """Heartbeat service configuration."""

    enabled: bool = True
    interval_s: int = 30 * 60  # 30 minutes


class GatewayConfig(Base):
    """Gateway/server configuration."""

    host: str = "0.0.0.0"
    port: int = 18790
    token: str = ""
    token_issue_path: str = "/api/auth/token"
    token_issue_secret: str = ""
    token_ttl_s: int = Field(default=300, ge=30, le=86_400)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)

    @field_validator("token_issue_path")
    @classmethod
    def token_issue_path_format(cls, value: str) -> str:
        value = value.strip()
        if not value:
            return ""
        if not value.startswith("/"):
            raise ValueError('token_issue_path must start with "/"')
        if len(value) > 1 and value.endswith("/"):
            return value.rstrip("/")
        return value


class WebSearchConfig(Base):
    """Web search tool configuration."""

    provider: str = "brave"  # brave, tavily, duckduckgo, searxng, jina
    api_key: str = ""
    base_url: str = ""  # SearXNG base URL
    max_results: int = 5


class WebToolsConfig(Base):
    """Web tools configuration."""

    proxy: str | None = (
        None  # HTTP/SOCKS5 proxy URL, e.g. "http://127.0.0.1:7890" or "socks5://127.0.0.1:1080"
    )
    search: WebSearchConfig = Field(default_factory=WebSearchConfig)


class ImageGenerationToolConfig(Base):
    """Image generation tool configuration."""

    enabled: bool = False
    provider: str = "openrouter"
    model: str = "openai/gpt-5.4-image-2"
    default_aspect_ratio: str = "1:1"
    default_image_size: str = "1K"
    max_images_per_turn: int = Field(default=4, ge=1, le=8)
    save_dir: str = "generated"


class CliAppsToolConfig(Base):
    """CLI Apps tool configuration."""

    enabled: bool = False
    run_timeout: int = Field(default=60, ge=1, le=600)


class ExecToolConfig(Base):
    """Shell exec tool configuration."""

    timeout: int = 60
    path_append: str = ""


class MCPServerConfig(Base):
    """MCP server connection configuration (stdio or HTTP)."""

    type: Literal["stdio", "sse", "streamableHttp"] | None = None  # auto-detected if omitted
    command: str = ""  # Stdio: command to run (e.g. "npx")
    args: list[str] = Field(default_factory=list)  # Stdio: command arguments
    env: dict[str, str] = Field(default_factory=dict)  # Stdio: extra env vars
    cwd: str = ""  # Stdio: working directory
    url: str = ""  # HTTP/SSE: endpoint URL
    headers: dict[str, str] = Field(default_factory=dict)  # HTTP/SSE: custom headers
    enabled_tools: list[str] = Field(default_factory=list)
    tool_timeout: int = 30  # seconds before a tool call is cancelled


class ToolsConfig(Base):
    """Tools configuration."""

    web: WebToolsConfig = Field(default_factory=WebToolsConfig)
    image_generation: ImageGenerationToolConfig = Field(default_factory=ImageGenerationToolConfig)
    cli_apps: CliAppsToolConfig = Field(default_factory=CliAppsToolConfig)
    exec: ExecToolConfig = Field(default_factory=ExecToolConfig)
    restrict_to_workspace: bool = False  # If true, restrict all tool access to workspace directory
    mcp_servers: dict[str, MCPServerConfig] = Field(default_factory=dict)


class Config(BaseSettings):
    """Root configuration for yuanclaw."""

    agents: AgentsConfig = Field(default_factory=AgentsConfig)
    channels: ChannelsConfig = Field(default_factory=ChannelsConfig)
    providers: ProvidersConfig = Field(default_factory=ProvidersConfig)
    gateway: GatewayConfig = Field(default_factory=GatewayConfig)
    tools: ToolsConfig = Field(default_factory=ToolsConfig)
    model_presets: dict[str, ModelPresetConfig] = Field(default_factory=dict)

    @property
    def workspace_path(self) -> Path:
        """Get expanded workspace path."""
        return Path(self.agents.defaults.workspace).expanduser()

    def resolve_preset(self, preset_name: str | None = None) -> ModelPresetConfig:
        """Resolve a named model preset or the defaults as a preset object."""
        if not preset_name:
            return self.agents.defaults.to_model_preset()
        try:
            return self.model_presets[preset_name]
        except KeyError as exc:
            raise KeyError(f"Unknown model preset: {preset_name}") from exc

    def _match_provider(
        self, model: str | None = None, preset: ModelPresetConfig | None = None
    ) -> tuple["ProviderConfig | None", str | None]:
        """Match provider config and its registry name. Returns (config, spec_name)."""
        from yuanclaw.providers.registry import PROVIDERS

        preset = preset or self.resolve_preset()
        forced = preset.provider
        if forced != "auto":
            p = getattr(self.providers, forced, None)
            return (p, forced) if p else (None, None)

        model_lower = (model or preset.model).lower()
        model_normalized = model_lower.replace("-", "_")
        model_prefix = model_lower.split("/", 1)[0] if "/" in model_lower else ""
        normalized_prefix = model_prefix.replace("-", "_")

        def _kw_matches(kw: str) -> bool:
            kw = kw.lower()
            return kw in model_lower or kw.replace("-", "_") in model_normalized

        def _provider_is_configured(
            p: ProviderConfig,
            spec_name: str,
            *,
            explicit_model_prefix: bool = False,
        ) -> bool:
            if spec_name == "bedrock":
                return explicit_model_prefix or bool(p.api_key or p.api_base or p.region or p.profile)
            return bool(p.api_key)

        # Explicit provider prefix wins — prevents `github-copilot/...codex` matching openai_codex.
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and model_prefix and normalized_prefix == spec.name:
                if (
                    spec.is_oauth
                    or spec.is_local
                    or _provider_is_configured(p, spec.name, explicit_model_prefix=True)
                ):
                    return p, spec.name

        # Match by keyword (order follows PROVIDERS registry)
        for spec in PROVIDERS:
            p = getattr(self.providers, spec.name, None)
            if p and any(_kw_matches(kw) for kw in spec.keywords):
                if spec.is_oauth or spec.is_local or _provider_is_configured(p, spec.name):
                    return p, spec.name

        # Local providers can route plain model names when api_base is configured.
        local_fallback: tuple[ProviderConfig, str] | None = None
        for spec in PROVIDERS:
            if not spec.is_local:
                continue
            p = getattr(self.providers, spec.name, None)
            if not (p and p.api_base):
                continue
            if spec.detect_by_base_keyword and spec.detect_by_base_keyword in p.api_base:
                return p, spec.name
            if local_fallback is None:
                local_fallback = (p, spec.name)
        if local_fallback:
            return local_fallback

        # Fallback: gateways first, then others (follows registry order)
        # OAuth providers are NOT valid fallbacks — they require explicit model selection
        for spec in PROVIDERS:
            if spec.is_oauth:
                continue
            p = getattr(self.providers, spec.name, None)
            if p and (
                _provider_is_configured(p, spec.name)
                or (spec.is_local and p.api_base)
            ):
                return p, spec.name
        return None, None

    def get_provider(
        self, model: str | None = None, preset: ModelPresetConfig | None = None
    ) -> ProviderConfig | None:
        """Get matched provider config (api_key, api_base, extra_headers). Falls back to first available."""
        p, _ = self._match_provider(model, preset=preset)
        return p

    def get_provider_name(
        self, model: str | None = None, preset: ModelPresetConfig | None = None
    ) -> str | None:
        """Get the registry name of the matched provider (e.g. "deepseek", "openrouter")."""
        _, name = self._match_provider(model, preset=preset)
        return name

    def get_api_key(
        self, model: str | None = None, preset: ModelPresetConfig | None = None
    ) -> str | None:
        """Get API key for the given model. Falls back to first available key."""
        p = self.get_provider(model, preset=preset)
        return p.api_key if p else None

    def get_api_base(
        self, model: str | None = None, preset: ModelPresetConfig | None = None
    ) -> str | None:
        """Get API base URL for the given model. Applies default URLs for known routed providers."""
        from yuanclaw.providers.registry import find_by_name

        p, name = self._match_provider(model, preset=preset)
        if p and p.api_base:
            return p.api_base
        if name:
            spec = find_by_name(name)
            if spec and spec.default_api_base:
                return spec.default_api_base
        return None

    model_config = ConfigDict(env_prefix="NANOBOT_", env_nested_delimiter="__")
