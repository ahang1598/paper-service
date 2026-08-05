# -*- coding: utf-8 -*-
"""真实 SiliconFlow reranker 集成测试（显式开关才运行）。"""

from __future__ import annotations

import os

import pytest

from academic_service.app.config import Settings
from academic_service.app.services.paper.reranker import SiliconFlowReranker


pytestmark = pytest.mark.siliconflow


def test_real_siliconflow_reranks_short_paper_fragments():
    if os.environ.get("RUN_SILICONFLOW_TESTS") != "1":
        pytest.skip("set RUN_SILICONFLOW_TESTS=1 to run real SiliconFlow test")

    settings = Settings()
    if not settings.siliconflow_api_key:
        pytest.skip("SILICONFLOW_API_KEY is not configured")

    reranker = SiliconFlowReranker(settings)
    documents = [
        "Intravenous epinephrine caused atrial fibrillation and requires continuous monitoring.",
        "Biodentine and mineral trioxide aggregate are materials used for indirect pulp treatment.",
        "The Young's modulus of multilayer WSe2 was measured as 167.3 ± 6.7 GPa.",
        "Articles you may be interested in include unrelated thin-film doping work.",
    ]
    ranked = reranker.rank("What risks can intravenous epinephrine cause?", documents)

    assert sorted(item.index for item in ranked) == list(range(len(documents)))
    assert [item.score for item in ranked] == sorted(
        [item.score for item in ranked], reverse=True
    )
    top_three = {item.index for item in ranked[:3]}
    assert 0 in top_three
