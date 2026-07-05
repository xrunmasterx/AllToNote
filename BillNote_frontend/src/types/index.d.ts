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
  available: boolean
  codex_bin?: string
  version?: string
  codex_home?: string
  auth_available?: boolean
  default_model?: string
  message?: string
}
