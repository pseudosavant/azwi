from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"

if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from azwi.config import resolve_config


class ConfigResolutionTests(unittest.TestCase):
    def test_resolve_config_layers_defaults_org_project_and_cli(self) -> None:
        raw_config = {
            "defaults": {
                "org": "main-org",
                "project": "DefaultProject",
                "fields": {
                    "acceptance": "Default.Acceptance",
                    "extra_fields": ["Custom.Default"],
                },
            },
            "projects": {
                "Payments": {
                    "fields": {
                        "description": "Custom.Description",
                        "extra_fields": ["Custom.Project"],
                    }
                }
            },
            "orgs": {
                "other-org": {
                    "defaults": {
                        "project": "OrgDefaultProject",
                        "fields": {
                            "system_info": "Custom.SystemInfo",
                            "extra_fields": ["Custom.Org"],
                        },
                    },
                    "projects": {
                        "Payments": {
                            "fields": {
                                "acceptance": "Custom.Acceptance",
                                "extra_fields": ["Custom.OrgProject"],
                            }
                        }
                    },
                }
            },
        }

        resolved = resolve_config(
            raw_config,
            env={"AZWI_ORG": "env-org", "AZWI_PROJECT": "EnvProject"},
            cli_org="other-org",
            resolved_project="Payments",
            cli_field_overrides={"description": "Cli.Description"},
            cli_extra_fields=["Custom.Cli"],
        )

        self.assertEqual(resolved.org, "other-org")
        self.assertEqual(resolved.project, "Payments")
        self.assertEqual(resolved.fields["description"], "Cli.Description")
        self.assertEqual(resolved.fields["acceptance"], "Custom.Acceptance")
        self.assertEqual(resolved.fields["system_info"], "Custom.SystemInfo")
        self.assertEqual(
            resolved.extra_fields,
            (
                "Custom.Default",
                "Custom.Org",
                "Custom.Project",
                "Custom.OrgProject",
                "Custom.Cli",
            ),
        )


if __name__ == "__main__":
    unittest.main()
