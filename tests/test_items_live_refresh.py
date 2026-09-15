from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_items_page_refreshes_results_without_reloading_the_page():
    template = (ROOT / "web_ui_plugin" / "templates" / "items.html").read_text(
        encoding="utf-8"
    )

    assert 'id="itemsResultsCount"' in template
    assert 'id="itemsPagination"' in template
    assert "function loadItemsFeed()" in template
    assert "fetch(window.location.href" in template
    assert "cache: 'no-store'" in template
    assert "window.setInterval(loadItemsFeed, 20000)" in template
    assert "document.addEventListener('visibilitychange'" in template
    assert "cardView.innerHTML = freshCardView.innerHTML" in template
    assert "itemsPagination.innerHTML = freshPagination.innerHTML" in template
    assert "window.location.reload" not in template


def test_items_refresh_preserves_page_state_and_avoids_unchanged_image_reloads():
    template = (ROOT / "web_ui_plugin" / "templates" / "items.html").read_text(
        encoding="utf-8"
    )

    assert "resultSignature(cardView) !== resultSignature(freshCardView)" in template
    assert "const listViewBody = listView.querySelector('tbody')" in template
    assert "window.scrollTo(scrollX, scrollY)" in template
    assert "updateRelativeTimes()" in template
    assert "updateNewBadges()" in template
