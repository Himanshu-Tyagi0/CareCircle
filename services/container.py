"""Service container assembling gateways, repositories, clock, and services safely."""

from __future__ import annotations

from dataclasses import dataclass

from core.clock import Clock, SystemClock
from core.config import AppConfig
from integrations.email_gateway import DemoEmailGateway, EmailGateway, SmtpEmailGateway
from integrations.gemini_gateway import DisabledGeminiGateway, GeminiGateway, GoogleGenAIGateway
from integrations.telegram_gateway import DemoTelegramGateway, HttpTelegramGateway, TelegramGateway
from repositories.demo_bundle import create_demo_repositories
from repositories.demo_store import DemoStore
from repositories.interfaces import Repositories
from scripts.seed_demo import seed_demo_store

from services.action_token_service import ActionTokenService
from services.ai_service import AIService
from services.appointment_service import AppointmentService
from services.auth_service import AuthService
from services.brief_service import BriefService
from services.checkin_service import CheckInService
from services.circle_service import CircleService
from services.dose_service import DoseService
from services.medication_service import MedicationService
from services.notification_service import NotificationService
from services.schedule_service import ScheduleService
from services.worker_service import WorkerService


@dataclass(frozen=True)
class ServiceContainer:
    """Shared stateless service boundary (never stores per-user session state)."""

    config: AppConfig
    clock: Clock
    repos: Repositories
    email_gateway: EmailGateway
    telegram_gateway: TelegramGateway
    gemini_gateway: GeminiGateway
    auth: AuthService
    circle: CircleService
    medication: MedicationService
    schedule: ScheduleService
    dose: DoseService
    appointment: AppointmentService
    checkin: CheckInService
    action_tokens: ActionTokenService
    notifications: NotificationService
    worker: WorkerService
    ai: AIService
    brief: BriefService


def build_container(
    config: AppConfig,
    *,
    clock: Clock | None = None,
    repos: Repositories | None = None,
    email_gateway: EmailGateway | None = None,
    telegram_gateway: TelegramGateway | None = None,
    gemini_gateway: GeminiGateway | None = None,
    seed_demo: bool = True,
) -> ServiceContainer:
    """Construct the application ServiceContainer respecting strict demo/prod separation."""
    clk: Clock = clock or SystemClock()

    if repos is None:
        if config.demo_mode:
            store = DemoStore()
            if seed_demo:
                seed_demo_store(store, clk.now(), rounds=config.bcrypt_rounds)
            _, repos = create_demo_repositories(store)
        else:
            config.require_production_ready()
            # Production path uses Postgres/Supabase repositories
            from repositories.postgres_repo import create_postgres_repositories

            repos = create_postgres_repositories(config)

    if email_gateway is None:
        if not config.demo_mode and config.has_smtp:
            email_gateway = SmtpEmailGateway(
                host=config.smtp_host,
                port=config.smtp_port,
                username=config.smtp_username,
                app_password=config.smtp_app_password,
                from_address=config.smtp_from_address,
                from_name=config.smtp_from_name,
                use_ssl=config.smtp_use_ssl,
                timeout_seconds=config.smtp_timeout_seconds,
            )
        else:
            email_gateway = DemoEmailGateway()

    if telegram_gateway is None:
        if not config.demo_mode and config.has_telegram:
            telegram_gateway = HttpTelegramGateway(
                bot_token=config.telegram_bot_token,
                timeout_seconds=config.telegram_timeout_seconds,
            )
        else:
            telegram_gateway = DemoTelegramGateway()

    if gemini_gateway is None:
        if not config.demo_mode and config.has_gemini:
            gemini_gateway = GoogleGenAIGateway(
                api_key=config.gemini_api_key, model=config.gemini_model
            )
        else:
            gemini_gateway = DisabledGeminiGateway()

    auth_svc = AuthService(
        auth_repo=repos.auth, email_gateway=email_gateway, config=config, clock=clk
    )
    circle_svc = CircleService(
        auth_repo=repos.auth,
        circle_repo=repos.circle,
        collab_repo=repos.collab,
        outbox_repo=repos.outbox,
        email_gateway=email_gateway,
        config=config,
        clock=clk,
    )
    token_svc = ActionTokenService(
        auth_repo=repos.auth,
        circle_repo=repos.circle,
        dose_repo=repos.dose,
        collab_repo=repos.collab,
        outbox_repo=repos.outbox,
        config=config,
        clock=clk,
    )
    notif_svc = NotificationService(
        auth_repo=repos.auth,
        circle_repo=repos.circle,
        dose_repo=repos.dose,
        collab_repo=repos.collab,
        outbox_repo=repos.outbox,
        token_service=token_svc,
        email_gateway=email_gateway,
        telegram_gateway=telegram_gateway,
        config=config,
        clock=clk,
    )
    med_svc = MedicationService(
        circle_repo=repos.circle,
        med_repo=repos.medication,
        dose_repo=repos.dose,
        clock=clk,
        notification_service=notif_svc,
    )
    sched_svc = ScheduleService(
        circle_repo=repos.circle,
        med_repo=repos.medication,
        dose_repo=repos.dose,
        clock=clk,
    )
    dose_svc = DoseService(
        circle_repo=repos.circle,
        dose_repo=repos.dose,
        med_service=med_svc,
        clock=clk,
        notification_service=notif_svc,
    )
    appt_svc = AppointmentService(collab_repo=repos.collab, clock=clk)
    checkin_svc = CheckInService(
        circle_repo=repos.circle,
        collab_repo=repos.collab,
        outbox_repo=repos.outbox,
        clock=clk,
        notification_service=notif_svc,
    )
    worker_svc = WorkerService(
        circle_repo=repos.circle,
        dose_repo=repos.dose,
        collab_repo=repos.collab,
        outbox_repo=repos.outbox,
        schedule_service=sched_svc,
        checkin_service=checkin_svc,
        notification_service=notif_svc,
        config=config,
        clock=clk,
    )
    ai_svc = AIService(
        gateway=gemini_gateway,
        circle_repo=repos.circle,
        med_repo=repos.medication,
        dose_repo=repos.dose,
        collab_repo=repos.collab,
        clock=clk,
    )
    brief_svc = BriefService(
        circle_repo=repos.circle,
        med_repo=repos.medication,
        dose_repo=repos.dose,
        collab_repo=repos.collab,
        outbox_repo=repos.outbox,
        ai_service=ai_svc,
        clock=clk,
        grace_minutes=config.adherence_grace_minutes,
    )

    return ServiceContainer(
        config=config,
        clock=clk,
        repos=repos,
        email_gateway=email_gateway,
        telegram_gateway=telegram_gateway,
        gemini_gateway=gemini_gateway,
        auth=auth_svc,
        circle=circle_svc,
        medication=med_svc,
        schedule=sched_svc,
        dose=dose_svc,
        appointment=appt_svc,
        checkin=checkin_svc,
        action_tokens=token_svc,
        notifications=notif_svc,
        worker=worker_svc,
        ai=ai_svc,
        brief=brief_svc,
    )
