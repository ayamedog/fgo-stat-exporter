from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_android_gradle_only_allows_the_non_packaging_proposal_task():
    gradle = (ROOT / "android/app/build.gradle").read_text(encoding="utf-8")
    hook = (
        ROOT / "tools/android_preaudit_candidate/package-input-hook.gradle"
    ).read_text(encoding="utf-8")

    assert "def runtimeAuditIntegrated = false" in gradle
    assert "RUNTIME_PREAUDIT_REQUIRED" in gradle
    assert (
        "requested.size() == 1 && "
        "requested[0] == ':app:proposeReleasePackageInputs'"
    ) in gradle
    assert "name.endsWith('.apk') || name.endsWith('.aab')" in gradle
    assert "PREAUDIT_PACKAGE_TASK_FORBIDDEN" in gradle
    assert "tasks.register('proposeReleasePackageInputs')" in hook
    assert "tasks.named('packageRelease').configure" in hook
    assert "verifyAndroidStageForPreaudit" in hook
    assert "PREAUDIT_CANDIDATE_ONLY" in hook
