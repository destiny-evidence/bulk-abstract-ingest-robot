# Variables below this line are for deploying the robot
# These may not be necessary for your use case

# --- Robot identity ---

variable "app_name" {
  description = "Name used to create Azure resources and as OTEL service name. The robot's own `robot_name` setting is derived from pyproject.toml, so it does not need to be set here."
}

variable "robot_id" {
  description = "The id the robot will send to destiny repository to identify itself (env: ROBOT_ID)."
}

variable "robot_secret" {
  description = "The secret the robot will use in HMAC auth with destiny repository (env: ROBOT_SECRET)."
  sensitive   = true
}

variable "destiny_repository_url" {
  description = "DESTinY repository API endpoint the robot talks to (env: BASE_URL)."
}

# --- Keycloak (SSO), used for reference lookups and enhancement requests ---

variable "keycloak_id" {
  description = "Keycloak client id (env: KEYCLOAK_ID)."
}

variable "keycloak_secret" {
  description = "Keycloak client secret (env: KEYCLOAK_SECRET)."
  sensitive   = true
}

variable "keycloak_url" {
  description = "Keycloak authentication url (env: KEYCLOAK_URL)."
  default     = "https://auth.evidence-repository.org/"
}

variable "keycloak_realm" {
  description = "Keycloak realm (env: KEYCLOAK_REALM)."
  default     = "destiny"
}

# --- References database (metadata cache) ---

variable "db_host" {
  description = "Host of the references database (env: DB_HOST)."
}

variable "db_port" {
  description = "Port of the references database (env: DB_PORT)."
  type        = number
  default     = 5432
}

variable "db_name" {
  description = "References database name (env: DB_NAME)."
  default     = "references"
}

variable "db_user" {
  description = "References database user (env: DB_USER)."
  default     = "pguser"
}

variable "db_password" {
  description = "References database password (env: DB_PASSWORD)."
  sensitive   = true
}

# --- Academic API importer tool (source of files to transfer) ---

variable "api_wrapper_endpoint" {
  description = "Public REST API of the academic-api tool, without trailing slash (env: API_WRAPPER_ENDPOINT)."
}

variable "api_wrapper_api_key" {
  description = "Authentication key for the academic-api tool API (env: API_WRAPPER_API_KEY)."
  sensitive   = true
}

# --- Container image registry ---

variable "container_registry_name" {
  description = "Name of the container registry where robot images are pushed."
}

variable "container_registry_resource_group_name" {
  description = "Name of the container registry resource group."
}

variable "environment" {
  description = "Environment for the robot, should be either development or staging (env: ENV)."
  default     = "staging"
}

# --- Observability ---

variable "honeycomb_endpoint" {
  description = "The Honeycomb API endpoint."
  default     = "https://api.honeycomb.io:443"
}

variable "honeycomb_api_key" {
  description = "The Honeycomb API key."
  sensitive   = true
}

# --- GitHub app auth (used by the github provider) ---

variable "github_app_id" {
  description = "The ID of the GitHub app used to authenticate the github provider."
}

variable "github_app_installation_id" {
  description = "The installation ID of the GitHub app used to authenticate the github provider."
}

variable "github_app_pem" {
  description = "The PEM file of the GitHub app used to authenticate the github provider."
  sensitive   = true
}
