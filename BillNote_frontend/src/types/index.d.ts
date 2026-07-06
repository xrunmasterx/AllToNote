export interface IProvider {
  id: string
  name: string
  logo: string
  type: string
  apiKey: string
  baseUrl: string
  enabled: number
}
export interface IResponse<T> {
  code: number
  data: T
  msg: string
}

export interface ICodexAppServerStatus {
  codex_cli_available: boolean
  codex_version?: string
  codex_bin?: string
  codex_home?: string
  auth_available?: boolean
  default_model?: string
  ready: boolean
  message?: string
}
