from app.services.integrations.apple import AppleCalendarIntegration
from app.services.integrations.base import IntegrationProvider
from app.services.integrations.google import GoogleIntegration
from app.services.integrations.jira import JiraIntegration
from app.services.integrations.notion import NotionIntegration
from app.services.integrations.yandex import YandexCalendarIntegration

PROVIDERS: dict[str, type[IntegrationProvider]] = {
    provider.slug: provider
    for provider in (GoogleIntegration, YandexCalendarIntegration, AppleCalendarIntegration, JiraIntegration, NotionIntegration)
}
