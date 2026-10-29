/* Trusted application code. Never interpolate excerpt text or source metadata. */
(() => {
    const requestId = "__REQUEST_ID__";
    const selector = "__TARGET_SELECTOR__";
    // Streamlit may insert this script more than once; handle each request only once.
    if (window.__tenderScrollRequest === requestId) return;
    window.__tenderScrollRequest = requestId;
    window.__tenderScrollCleanup?.(); // Stop any earlier request still waiting for its target.

    let finished = false;
    let observer;
    let timeout;
    const cancel = () => {
        finished = true;
        observer?.disconnect();
        clearTimeout(timeout);
        for (const event of ["wheel", "touchstart", "keydown"]) {
            document.removeEventListener(event, cancel, true);
        }
    };
    window.__tenderScrollCleanup = cancel;

    const reveal = () => {
        if (finished) return; // Ignore callbacks left in the animation-frame queue after cancellation.
        const target = document.querySelector(selector);
        if (!target || target.getClientRects().length === 0) return;
        const bounds = target.getBoundingClientRect();
        if (bounds.height === 0) return;
        // Leave an already visible result in place on a split layout. Its top
        // must clear the header (72px) and stay within the smaller of 280px
        // or 45% of the viewport height.
        const comfortablyVisible = bounds.top >= 72 && bounds.top <= Math.min(280, innerHeight * 0.45);
        if (!comfortablyVisible) {
            const reducedMotion = matchMedia("(prefers-reduced-motion: reduce)").matches;
            target.scrollIntoView({block: "start", behavior: reducedMotion ? "instant" : "smooth"});
        }
        const focusTarget = target.querySelector('[role="tab"][aria-selected="true"], h2, h3');
        if (focusTarget) {
            // Let code focus a heading without adding it to the Tab-key sequence.
            if (focusTarget.matches("h2, h3")) focusTarget.setAttribute("tabindex", "-1");
            focusTarget.focus({preventScroll: true}); // Avoid a second scroll when transferring keyboard focus.
        }
        cancel();
    };
    // Give the browser two animation frames to lay out new content before measuring it.
    const afterLayout = () => requestAnimationFrame(() => requestAnimationFrame(reveal));
    observer = new MutationObserver(afterLayout);
    // The target may not exist yet; watch the document until Streamlit inserts it.
    observer.observe(document.body, {childList: true, subtree: true, attributes: true});
    // Stop waiting after three seconds so a missing target cannot leave observers and listeners active.
    timeout = setTimeout(cancel, 3000);
    // Respect a user who starts scrolling or using the keyboard before rendering completes.
    for (const event of ["wheel", "touchstart", "keydown"]) {
        // Capture input before widget handlers; leave its default browser action alone.
        document.addEventListener(event, cancel, {capture: true, passive: true});
    }
    afterLayout();
})();
