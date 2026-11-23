# This deploys the robot as a container application into Azure. You may choose to do this differently.

# This is where the container app will pull images from
data "azurerm_container_registry" "destiny_shared_infra" {
  name                = var.container_registry_name
  resource_group_name = var.container_registry_resource_group_name
}

# This might exist for you if your robot has already been deployed.
# In this case, you can use a data resource instead https://registry.terraform.io/providers/hashicorp/azurerm/latest/docs/data-sources/resource_group
resource "azurerm_resource_group" "this" {
  name     = "rg-${var.app_name}-${var.environment}"
  location = "swedencentral"
}

# Create a user assigned identity for our robot. This is the identity used when authenticating.
resource "azurerm_user_assigned_identity" "this" {
  location            = azurerm_resource_group.this.location
  name                = var.app_name
  resource_group_name = azurerm_resource_group.this.name
}

# This creates a container app to run the robot in
module "container_app" {
  source                          = "app.terraform.io/destiny-evidence/container-app/azure"
  version                         = "1.6.2"
  app_name                        = var.app_name
  environment                     = var.environment
  container_registry_id           = data.azurerm_container_registry.destiny_shared_infra.id
  container_registry_login_server = data.azurerm_container_registry.destiny_shared_infra.login_server
  resource_group_name             = azurerm_resource_group.this.name
  region                          = azurerm_resource_group.this.location

  # Environment variables are read by the robot's pydantic settings (app/util/config.py).
  # The necessary `AZURE_CLIENT_ID` environment variable is set by the container app module.
  env_vars = [
    {
      name  = "ENV"
      value = var.environment
    },
    {
      name  = "BASE_URL"
      value = var.destiny_repository_url
    },
    {
      name  = "ROBOT_ID"
      value = var.robot_id
    },
    {
      name        = "ROBOT_SECRET"
      secret_name = "robot-secret"
    },
    {
      name  = "KEYCLOAK_ID"
      value = var.keycloak_id
    },
    {
      name  = "KEYCLOAK_URL"
      value = var.keycloak_url
    },
    {
      name  = "KEYCLOAK_REALM"
      value = var.keycloak_realm
    },
    {
      name        = "KEYCLOAK_SECRET"
      secret_name = "keycloak-secret"
    },
    {
      name  = "DB_HOST"
      value = var.db_host
    },
    {
      name  = "DB_PORT"
      value = tostring(var.db_port)
    },
    {
      name  = "DB_NAME"
      value = var.db_name
    },
    {
      name  = "DB_USER"
      value = var.db_user
    },
    {
      name        = "DB_PASSWORD"
      secret_name = "db-password"
    },
    {
      name  = "OTEL_SERVICE_NAME",
      value = "${var.app_name}-${var.environment}"
    },
    {
      name  = "OTEL_TRACES_EXPORTER",
      value = "otlp"
    },
    {
      name  = "OTEL_LOGS_EXPORTER",
      value = "otlp"
    },
    {
      name  = "OTEL_EXPORTER_OTLP_PROTOCOL",
      value = "http/protobuf"
    },
    {
      name  = "OTEL_EXPORTER_OTLP_ENDPOINT",
      value = var.honeycomb_endpoint
    },
    {
      name        = "OTEL_EXPORTER_OTLP_HEADERS",
      secret_name = "honeycomb-api-header"
    },
    {
      name        = "OTEL_EXPORTER_OTLP_METRICS_HEADERS",
      secret_name = "honeycomb-metrics-api-header"
    },
    {
      name  = "OTEL_PYTHON_LOGGING_AUTO_INSTRUMENTATION_ENABLED",
      value = "true"
    }
  ]

  secrets = [
    {
      name  = "robot-secret",
      value = var.robot_secret
    },
    {
      name  = "keycloak-secret",
      value = var.keycloak_secret
    },
    {
      name  = "db-password",
      value = var.db_password
    },
    {
      name  = "honeycomb-api-header",
      value = "x-honeycomb-team=${var.honeycomb_api_key}"
    },
    {
      name  = "honeycomb-metrics-api-header",
      value = "x-honeycomb-team=${var.honeycomb_api_key},x-honeycomb-dataset=metrics-robots"
    }
  ]

  # You can see here that we're passing the user assigned identity that we created above to the client application.
  # This identity has the robot role assignment and will allow the robot to authenticate with destiny repository.
  identity = {
    id           = azurerm_user_assigned_identity.this.id
    principal_id = azurerm_user_assigned_identity.this.principal_id
    client_id    = azurerm_user_assigned_identity.this.client_id
  }
}
