(() => {
  const padRank = (index) => String(index + 1).padStart(2, "0");

  const updateCardScrollers = (root = document) => {
    root.querySelectorAll("[data-card-scroll]").forEach((scroller) => {
      scroller.dispatchEvent(new CustomEvent("card-scroll:update"));
    });
  };

  const initCardScrollers = (root = document) => {
    root.querySelectorAll("[data-card-scroll]").forEach((scroller) => {
      if (scroller.dataset.cardScrollReady === "1") {
        return;
      }
      const strip = scroller.querySelector("[data-card-scroll-strip]");
      const leftButton = scroller.querySelector("[data-card-scroll-left]");
      const rightButton = scroller.querySelector("[data-card-scroll-right]");
      if (!(strip instanceof HTMLElement)) {
        return;
      }
      scroller.dataset.cardScrollReady = "1";

      const update = () => {
        const maxScroll = Math.max(0, strip.scrollWidth - strip.clientWidth);
        const hasOverflow = maxScroll > 1;
        scroller.classList.toggle("has-overflow", hasOverflow);
        scroller.classList.toggle("at-start", !hasOverflow || strip.scrollLeft <= 1);
        scroller.classList.toggle("at-end", !hasOverflow || strip.scrollLeft >= maxScroll - 1);
        if (leftButton instanceof HTMLButtonElement) {
          leftButton.disabled = !hasOverflow || strip.scrollLeft <= 1;
        }
        if (rightButton instanceof HTMLButtonElement) {
          rightButton.disabled = !hasOverflow || strip.scrollLeft >= maxScroll - 1;
        }
      };

      const scrollByPage = (direction) => {
        const distance = Math.max(220, Math.floor(strip.clientWidth * 0.86));
        strip.scrollBy({ left: distance * direction, behavior: "smooth" });
      };

      if (leftButton instanceof HTMLButtonElement) {
        leftButton.addEventListener("click", () => scrollByPage(-1));
      }
      if (rightButton instanceof HTMLButtonElement) {
        rightButton.addEventListener("click", () => scrollByPage(1));
      }

      let dragState = null;
      strip.addEventListener("pointerdown", (event) => {
        if (event.pointerType === "mouse" && event.button !== 0) {
          return;
        }
        dragState = {
          pointerId: event.pointerId,
          startX: event.clientX,
          scrollLeft: strip.scrollLeft,
        };
        scroller.classList.add("is-dragging");
        strip.setPointerCapture(event.pointerId);
      });
      strip.addEventListener("pointermove", (event) => {
        if (!dragState || dragState.pointerId !== event.pointerId) {
          return;
        }
        const delta = event.clientX - dragState.startX;
        strip.scrollLeft = dragState.scrollLeft - delta;
        if (Math.abs(delta) > 2) {
          event.preventDefault();
        }
      });
      const finishDrag = (event) => {
        if (!dragState || dragState.pointerId !== event.pointerId) {
          return;
        }
        dragState = null;
        scroller.classList.remove("is-dragging");
        if (strip.hasPointerCapture(event.pointerId)) {
          strip.releasePointerCapture(event.pointerId);
        }
      };
      strip.addEventListener("pointerup", finishDrag);
      strip.addEventListener("pointercancel", finishDrag);
      strip.addEventListener("scroll", update, { passive: true });
      scroller.addEventListener("card-scroll:update", update);
      if ("ResizeObserver" in window) {
        new ResizeObserver(update).observe(strip);
      }
      requestAnimationFrame(update);
    });
  };

  const initVariantViewers = (root = document) => {
    root.querySelectorAll("[data-variant-root]").forEach((viewer) => {
      if (viewer.dataset.variantReady === "1") {
        return;
      }
      viewer.dataset.variantReady = "1";
      const variants = Array.from(viewer.querySelectorAll("[data-variant]"));
      const button = viewer.querySelector("[data-variant-button]");
      const label = viewer.querySelector("[data-variant-label]");
      let current = 0;
      if (!variants.length) {
        if (label) {
          label.textContent = "构筑 0/0";
        }
        return;
      }
      const render = () => {
        variants.forEach((variant, index) => {
          variant.classList.toggle("is-active", index === current);
        });
        if (label) {
          label.textContent = `构筑 ${current + 1}/${variants.length}`;
        }
        initCardScrollers(viewer);
        updateCardScrollers(viewer);
      };
      if (button) {
        button.addEventListener("click", () => {
          current = (current + 1) % variants.length;
          render();
        });
      }
      render();
    });
  };

  const initSortToolbars = () => {
    document.querySelectorAll("[data-sort-toolbar]").forEach((toolbar) => {
      if (toolbar.dataset.sortReady === "1") {
        return;
      }
      toolbar.dataset.sortReady = "1";
      const targetId = toolbar.getAttribute("data-sort-target");
      const root = targetId ? document.getElementById(targetId) : null;
      if (!root) {
        return;
      }
      const buttons = Array.from(toolbar.querySelectorAll("[data-sort-button]"));
      const metricValue = (item, key) => Number(item.getAttribute(`data-sort-${key}`) || 0);
      const applySort = (key) => {
        root.querySelectorAll("[data-sort-item]").forEach((item, index) => {
          if (!item.dataset.sortOriginal) {
            item.dataset.sortOriginal = String(index);
          }
        });
        const secondaryKey = key === "wilson" ? "sample" : "wilson";
        const sortedItems = Array.from(root.querySelectorAll("[data-sort-item]")).sort((left, right) => {
          const primaryDiff = metricValue(right, key) - metricValue(left, key);
          if (primaryDiff !== 0) {
            return primaryDiff;
          }
          const secondaryDiff = metricValue(right, secondaryKey) - metricValue(left, secondaryKey);
          if (secondaryDiff !== 0) {
            return secondaryDiff;
          }
          return Number(left.dataset.sortOriginal || 0) - Number(right.dataset.sortOriginal || 0);
        });
        sortedItems.forEach((item, index) => {
          const rank = item.querySelector("[data-rank-value]");
          if (rank) {
            rank.textContent = padRank(index);
          }
          root.appendChild(item);
        });
        buttons.forEach((button) => {
          button.setAttribute("aria-pressed", String(button.getAttribute("data-sort-key") === key));
        });
      };
      toolbar.addEventListener("click", (event) => {
        const target = event.target instanceof Element ? event.target : null;
        const button = target ? target.closest("[data-sort-button]") : null;
        if (!button) {
          return;
        }
        applySort(button.getAttribute("data-sort-key") || "sample");
      });
      toolbar.dataset.applySort = "sample";
    });
  };

  const activeSortKey = () => {
    const active = document.querySelector("[data-sort-button][aria-pressed='true']");
    return active ? active.getAttribute("data-sort-key") || "sample" : "sample";
  };

  const resortCurrentBoard = () => {
    const active = document.querySelector("[data-sort-button][aria-pressed='true']");
    if (active instanceof HTMLButtonElement) {
      active.click();
    }
  };

  const initLoadMore = () => {
    const control = document.querySelector("[data-load-more]");
    if (!control || control.dataset.loadMoreReady === "1") {
      return;
    }
    control.dataset.loadMoreReady = "1";
    const button = control.querySelector("[data-load-more-button]");
    const status = control.querySelector("[data-load-more-status]");
    const targetId = control.getAttribute("data-target");
    const root = targetId ? document.getElementById(targetId) : null;
    if (!(button instanceof HTMLButtonElement) || !root) {
      return;
    }

    const setStatus = (text) => {
      if (status) {
        status.textContent = text;
      }
    };

    button.addEventListener("click", async () => {
      const endpoint = control.getAttribute("data-endpoint") || "";
      const offset = Number(control.getAttribute("data-next-offset") || "0");
      const pageSize = Number(control.getAttribute("data-page-size") || "80");
      if (!endpoint || !Number.isFinite(offset) || !Number.isFinite(pageSize)) {
        return;
      }
      const url = new URL(endpoint, window.location.origin);
      url.searchParams.set("offset", String(offset));
      url.searchParams.set("limit", String(pageSize));
      url.searchParams.set("sort", activeSortKey());
      button.disabled = true;
      setStatus("加载中...");
      try {
        const response = await fetch(url, { credentials: "same-origin" });
        if (!response.ok) {
          throw new Error(`HTTP ${response.status}`);
        }
        const payload = await response.json();
        const fragment = document.createElement("template");
        fragment.innerHTML = payload.html || "";
        root.appendChild(fragment.content);
        initVariantViewers(root);
        initCardScrollers(root);
        control.setAttribute("data-next-offset", String(payload.next_offset || offset + pageSize));
        if (payload.has_more) {
          setStatus(`已显示 ${payload.next_offset} / ${payload.total}`);
          button.disabled = false;
        } else {
          control.classList.add("is-complete");
          setStatus(`已全部显示 ${payload.total} 条`);
        }
        resortCurrentBoard();
      } catch (error) {
        button.disabled = false;
        setStatus("加载失败，请稍后重试");
      }
    });
  };

  const boot = () => {
    initVariantViewers();
    initCardScrollers();
    initSortToolbars();
    initLoadMore();
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", boot, { once: true });
  } else {
    boot();
  }
})();
