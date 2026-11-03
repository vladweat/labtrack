import pytest
from sqlalchemy.exc import SQLAlchemyError

from app import cli


def test_provision_user_parser_requires_one_scope_and_accepts_no_password_argument() -> None:
    args = cli.build_parser().parse_args(
        [
            "provision-user",
            "--email",
            "sender@example.test",
            "--display-name",
            "Sender",
            "--role",
            "SENDER_OPERATOR",
            "--organization-code",
            "PILOT_SENDER",
        ]
    )

    assert args.organization_code == "PILOT_SENDER"
    assert args.region_code is None
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            [
                "provision-user",
                "--email",
                "sender@example.test",
                "--display-name",
                "Sender",
                "--role",
                "SENDER_OPERATOR",
                "--organization-code",
                "PILOT_SENDER",
                "--password",
                "secret",
            ]
        )


def test_local_role_fixture_command_has_no_credential_arguments() -> None:
    args = cli.build_parser().parse_args(["provision-local-roles"])
    assert args.command == "provision-local-roles"
    with pytest.raises(SystemExit):
        cli.build_parser().parse_args(
            ["provision-local-roles", "--password", "must-not-be-an-argument"]
        )


def test_password_prompt_confirms_password_without_echoing(monkeypatch) -> None:
    prompts: list[str] = []
    passwords = iter(["a sufficiently long password", "a sufficiently long password"])

    def hidden_prompt(prompt: str) -> str:
        prompts.append(prompt)
        return next(passwords)

    class InteractiveInput:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(cli.getpass, "getpass", hidden_prompt)
    monkeypatch.setattr(cli.sys, "stdin", InteractiveInput())

    assert cli._prompt_password() == "a sufficiently long password"
    assert len(prompts) == 2
    assert all("hidden" in prompt.lower() or "confirm" in prompt.lower() for prompt in prompts)


def test_password_prompt_requires_a_terminal(monkeypatch) -> None:
    class NonInteractiveInput:
        @staticmethod
        def isatty() -> bool:
            return False

    monkeypatch.setattr(cli.sys, "stdin", NonInteractiveInput())

    with pytest.raises(cli.ProvisioningError, match="interactive terminal"):
        cli._prompt_password()


def test_password_prompt_rejects_mismatch(monkeypatch) -> None:
    passwords = iter(["a sufficiently long password", "a different long password"])

    class InteractiveInput:
        @staticmethod
        def isatty() -> bool:
            return True

    monkeypatch.setattr(cli.getpass, "getpass", lambda _: next(passwords))
    monkeypatch.setattr(cli.sys, "stdin", InteractiveInput())

    with pytest.raises(cli.ProvisioningError, match="do not match"):
        cli._prompt_password()


def test_database_error_output_does_not_expose_sql_details(monkeypatch, capsys) -> None:
    async def fail_without_database(*_args, **_kwargs):
        raise SQLAlchemyError("sensitive database detail")

    monkeypatch.setattr(cli, "_execute", fail_without_database)

    result = cli.main(
        [
            "grant-membership",
            "--email",
            "manager@example.test",
            "--role",
            "REGIONAL_OPERATIONS",
            "--region-code",
            "PILOT",
        ]
    )

    captured = capsys.readouterr()
    assert result == 2
    assert "database operation failed" in captured.err
    assert "sensitive database detail" not in captured.err
