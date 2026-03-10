"""
Pydantic models for API request/response types.

Contains:
- OpenAI Chat Completion models (request, response, streaming)
- Gemini models (request, response, generation config)
- Common models (Model, ModelList, Error)
- Control Panel models
- Authentication models

Extracted from gcli2api/src/models.py for akarins-gateway.
"""

from typing import Any, Dict, List, Optional, Union

from pydantic import BaseModel, Field, field_validator, model_validator


# Pydantic v1/v2 兼容性辅助函数
def model_to_dict(model: BaseModel) -> Dict[str, Any]:
    """
    兼容 Pydantic v1 和 v2 的模型转字典方法
    - v1: model.dict()
    - v2: model.model_dump()
    """
    if hasattr(model, 'model_dump'):
        # Pydantic v2
        return model.model_dump()
    else:
        # Pydantic v1
        return model.dict()


# Common Models
class Model(BaseModel):
    id: str
    object: str = "model"
    created: Optional[int] = None
    owned_by: Optional[str] = "google"


class ModelList(BaseModel):
    object: str = "list"
    data: List[Model]


# OpenAI Models
class OpenAIToolFunction(BaseModel):
    name: str
    arguments: str  # JSON string


class OpenAIToolCall(BaseModel):
    id: str
    type: str = "function"
    function: OpenAIToolFunction
    index: Optional[int] = None  # 用于流式响应的工具调用索引


class OpenAITool(BaseModel):
    type: str = "function"
    function: Dict[str, Any]


class OpenAIChatMessage(BaseModel):
    role: str
    content: Union[str, List[Dict[str, Any]], None] = None
    reasoning_content: Optional[str] = None
    name: Optional[str] = None
    tool_calls: Optional[List[OpenAIToolCall]] = None
    tool_call_id: Optional[str] = None  # for role="tool"

    @field_validator('tool_calls', mode='before')
    @classmethod
    def normalize_tool_calls(cls, v):
        """
        [FIX 2026-02-07] 修复 Cursor IDE 流式累积 tool_calls 格式问题

        Cursor IDE 发送的流式请求中 tool_calls 可能以 dict 格式累积:
        {'0': {'index': 0, 'id': '...', ...}, '1': {...}}

        但 Pydantic 验证需要 list 格式:
        [{'index': 0, 'id': '...', ...}, {...}]

        此验证器在 Pydantic 解析前自动转换格式
        """
        if v is None:
            return v

        # 如果已经是 list，直接返回
        if isinstance(v, list):
            return v

        # 如果是 dict 格式（流式累积格式），转换为 list
        if isinstance(v, dict):
            # 按 key 排序后提取 values，保持顺序
            try:
                sorted_keys = sorted(v.keys(), key=lambda x: int(x) if str(x).isdigit() else x)
                return [v[k] for k in sorted_keys]
            except (ValueError, TypeError):
                # 如果排序失败，直接返回 values
                return list(v.values())

        # 其他情况原样返回，让 Pydantic 后续验证处理
        return v


class OpenAIChatCompletionRequest(BaseModel):
    model: str
    messages: List[OpenAIChatMessage]
    stream: bool = False
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    top_p: Optional[float] = Field(None, ge=0.0, le=1.0)
    max_tokens: Optional[int] = Field(None, ge=1)
    stop: Optional[Union[str, List[str]]] = None
    frequency_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    presence_penalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    n: Optional[int] = Field(1, ge=1, le=128)
    seed: Optional[int] = None
    response_format: Optional[Dict[str, Any]] = None
    top_k: Optional[int] = Field(None, ge=1)
    enable_anti_truncation: Optional[bool] = False
    tools: Optional[List[OpenAITool]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None

    class Config:
        extra = "allow"  # Allow additional fields not explicitly defined


# 通用的聊天完成请求模型（兼容OpenAI和其他格式）
ChatCompletionRequest = OpenAIChatCompletionRequest


class OpenAIChatCompletionChoice(BaseModel):
    index: int
    message: OpenAIChatMessage
    finish_reason: Optional[str] = None
    logprobs: Optional[Dict[str, Any]] = None


class OpenAIChatCompletionResponse(BaseModel):
    id: str
    object: str = "chat.completion"
    created: int
    model: str
    choices: List[OpenAIChatCompletionChoice]
    usage: Optional[Dict[str, int]] = None
    system_fingerprint: Optional[str] = None


class OpenAIDelta(BaseModel):
    role: Optional[str] = None
    content: Optional[str] = None
    reasoning_content: Optional[str] = None


class OpenAIChatCompletionStreamChoice(BaseModel):
    index: int
    delta: OpenAIDelta
    finish_reason: Optional[str] = None
    logprobs: Optional[Dict[str, Any]] = None


class OpenAIChatCompletionStreamResponse(BaseModel):
    id: str
    object: str = "chat.completion.chunk"
    created: int
    model: str
    choices: List[OpenAIChatCompletionStreamChoice]
    system_fingerprint: Optional[str] = None


# Gemini Models
class GeminiPart(BaseModel):
    text: Optional[str] = None
    inlineData: Optional[Dict[str, Any]] = None
    fileData: Optional[Dict[str, Any]] = None
    thought: Optional[bool] = False


class GeminiContent(BaseModel):
    role: str
    parts: List[GeminiPart]


class GeminiSystemInstruction(BaseModel):
    parts: List[GeminiPart]


class GeminiImageConfig(BaseModel):
    """图片生成配置"""
    aspect_ratio: Optional[str] = None
    image_size: Optional[str] = None


class GeminiGenerationConfig(BaseModel):
    temperature: Optional[float] = Field(None, ge=0.0, le=2.0)
    topP: Optional[float] = Field(None, ge=0.0, le=1.0)
    topK: Optional[int] = Field(None, ge=1)
    maxOutputTokens: Optional[int] = Field(None, ge=1)
    stopSequences: Optional[List[str]] = None
    responseMimeType: Optional[str] = None
    responseSchema: Optional[Dict[str, Any]] = None
    candidateCount: Optional[int] = Field(None, ge=1, le=8)
    seed: Optional[int] = None
    frequencyPenalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    presencePenalty: Optional[float] = Field(None, ge=-2.0, le=2.0)
    thinkingConfig: Optional[Dict[str, Any]] = None
    # 图片生成相关参数
    response_modalities: Optional[List[str]] = None  # ["TEXT", "IMAGE"]
    image_config: Optional[GeminiImageConfig] = None


class GeminiSafetySetting(BaseModel):
    category: str
    threshold: str


class GeminiRequest(BaseModel):
    contents: List[GeminiContent]
    systemInstruction: Optional[GeminiSystemInstruction] = None
    generationConfig: Optional[GeminiGenerationConfig] = None
    safetySettings: Optional[List[GeminiSafetySetting]] = None
    tools: Optional[List[Dict[str, Any]]] = None
    toolConfig: Optional[Dict[str, Any]] = None
    cachedContent: Optional[str] = None
    enable_anti_truncation: Optional[bool] = False

    class Config:
        extra = "allow"  # 允许透传未定义的字段


class GeminiCandidate(BaseModel):
    content: GeminiContent
    finishReason: Optional[str] = None
    index: int = 0
    safetyRatings: Optional[List[Dict[str, Any]]] = None
    citationMetadata: Optional[Dict[str, Any]] = None
    tokenCount: Optional[int] = None


class GeminiUsageMetadata(BaseModel):
    promptTokenCount: Optional[int] = None
    candidatesTokenCount: Optional[int] = None
    totalTokenCount: Optional[int] = None


class GeminiResponse(BaseModel):
    candidates: List[GeminiCandidate]
    usageMetadata: Optional[GeminiUsageMetadata] = None
    modelVersion: Optional[str] = None


# Error Models
class APIError(BaseModel):
    message: str
    type: str = "api_error"
    code: Optional[int] = None


class ErrorResponse(BaseModel):
    error: APIError


# Control Panel Models
class SystemStatus(BaseModel):
    status: str
    timestamp: str
    credentials: Dict[str, int]
    config: Dict[str, Any]
    current_credential: str


class CredentialInfo(BaseModel):
    filename: str
    project_id: Optional[str] = None
    status: Dict[str, Any]
    size: Optional[int] = None
    modified_time: Optional[str] = None
    error: Optional[str] = None


class LogEntry(BaseModel):
    timestamp: str
    level: str
    message: str
    module: Optional[str] = None


class ConfigValue(BaseModel):
    key: str
    value: Any
    env_locked: bool = False
    description: Optional[str] = None


# Authentication Models
class AuthRequest(BaseModel):
    project_id: Optional[str] = None
    user_session: Optional[str] = None


class AuthResponse(BaseModel):
    success: bool
    auth_url: Optional[str] = None
    state: Optional[str] = None
    error: Optional[str] = None
    credentials: Optional[Dict[str, Any]] = None
    file_path: Optional[str] = None
    requires_manual_project_id: Optional[bool] = None
    requires_project_selection: Optional[bool] = None
    available_projects: Optional[List[Dict[str, str]]] = None


class CredentialStatus(BaseModel):
    disabled: bool = False
    error_codes: List[int] = []
    last_success: Optional[str] = None


# Web Routes Models
class LoginRequest(BaseModel):
    password: str


class AuthStartRequest(BaseModel):
    project_id: Optional[str] = None
    use_antigravity: Optional[bool] = False


class AuthCallbackRequest(BaseModel):
    project_id: Optional[str] = None
    use_antigravity: Optional[bool] = False


class AuthCallbackUrlRequest(BaseModel):
    callback_url: str
    project_id: Optional[str] = None
    use_antigravity: Optional[bool] = False


class CredFileActionRequest(BaseModel):
    filename: str
    action: str  # enable, disable, delete


class CredFileBatchActionRequest(BaseModel):
    action: str  # "enable", "disable", "delete"
    filenames: List[str]  # 批量操作的文件名列表


class ConfigSaveRequest(BaseModel):
    config: dict
