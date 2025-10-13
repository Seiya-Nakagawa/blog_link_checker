variable "aws_region" {
  description = "デプロイするAWSリージョン"
  type        = string
}

variable "system_name" {
  description = "システム識別子"
  type        = string
}

variable "env" {
  description = "環境識別子"
  type        = string
}

variable "aws_account_id" {
  description = "AWSアカウントID"
  type        = string
}

variable "lambda_runtime_version" {
  description = "Lambdaランタイムのバージョン"
  type        = list(string)
}

variable "lambda_timeout_seconds" {
  description = "Lambdaのタイムアウト（秒）"
  type        = number
}

variable "lambda_memory_size" {
  description = "Lambdaのメモリサイズ（MB）"
  type        = number
}

variable "lambda_log_level" {
  description = "Lambdaのログレベル"
  type        = string
}

variable "lambda_request_timeout" {
  description = "リクエストのタイムアウト"
  type        = number
}

variable "lambda_max_retries" {
  description = "最大リトライ回数"
  type        = number
}

variable "lambda_backoff_factor" {
  description = "バックオフ係数"
  type        = number
}

variable "lambda_max_workers" {
  description = "最大ワーカー数"
  type        = number
}

variable "lambda_crawl_wait_seconds" {
  description = "クロール待機時間（秒）"
  type        = number
}

variable "lambda_ng_words" {
  description = "NGワード（カンマ区切り）"
  type        = string
}

variable "lambda_exclude_strings" {
  description = "リンクチェックから除外する文字列（カンマ区切り）"
  type        = string
}

variable "notification_emails_blog" {
  description = "通知を受け取るメールアドレスのリスト"
  type        = list(string)
  default     = []
}