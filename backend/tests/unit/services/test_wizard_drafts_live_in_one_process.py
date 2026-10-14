"""Wizard drafts are held in the memory of the process that made them.

A draft made through ``ExperimentWizardService`` (what the wizard routes call)
is found again in the same process, and not in a second Python process started
afterwards, with the same environment, from the same checkout: that is what an
API restart, or another API replica behind the load balancer, sees.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from backend.app.services.experiment_wizard_service import ExperimentWizardService

pytestmark = [pytest.mark.unit, pytest.mark.regression]

REPO_ROOT = Path(__file__).resolve().parents[4]

READ_BACK = """
import json, sys
from backend.app.services.experiment_wizard_service import ExperimentWizardService
draft_id, user_id = sys.argv[1], sys.argv[2]
found = ExperimentWizardService.get_draft(draft_id, user_id)
print(json.dumps({
    "found": found is not None,
    "listed": [d.id for d in ExperimentWizardService.list_drafts(user_id)],
}))
"""


def test_a_draft_is_not_seen_by_another_process():
    user_id = "wizard-memory-user"
    draft = ExperimentWizardService.create_draft(user_id, "ab")
    ExperimentWizardService.update_draft(
        draft.id, user_id, "choose_type", {"experiment_type": "multivariate"}
    )
    try:
        here = ExperimentWizardService.get_draft(draft.id, user_id)
        assert here is not None and here.experiment_type == "multivariate"

        result = subprocess.run(
            [sys.executable, "-c", READ_BACK, draft.id, user_id],
            cwd=REPO_ROOT,
            env=dict(os.environ),
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert result.returncode == 0, result.stderr[-2000:]
        answer = json.loads(result.stdout.strip().splitlines()[-1])
        assert answer == {"found": False, "listed": []}
    finally:
        ExperimentWizardService.delete_draft(draft.id, user_id)
