variable "db_password" {
  description = "Password for the database user (set in terraform.tfvars, never here)"
  type        = string
  sensitive   = true
}

variable "api_token_ttl" {
  description = "Token lifetime in seconds"
  type        = number
  default     = 3600
}

variable "secret_name" {
  type    = string
  default = "prod/app/db"
}

resource "aws_secretsmanager_secret" "db" {
  name        = var.secret_name
  description = "Database password"
  recovery_window_in_days = 7
}

resource "aws_ssm_parameter" "max_tokens" {
  name  = "/app/max_tokens"
  type  = "String"
  value = "4096"
}

resource "aws_ssm_parameter" "db_password" {
  name  = "/app/db_password"
  type  = "SecureString"
  value = var.db_password
}

resource "aws_dynamodb_table" "t" {
  name     = "orders"
  hash_key = "order_id"
  range_key = "created_at"
  attribute {
    name = "order_id"
    type = "S"
  }
}

output "token_endpoint" {
  value = "https://auth.example.invalid/token"
}
