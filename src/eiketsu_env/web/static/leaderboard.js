(() => {
  const padRank = (index) => String(index + 1).padStart(2, "0");

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
        applySort(button.getAttribute("data-sort-key") || "wilson");
      });
      toolbar.dataset.applySort = "wilson";
    });
  };

  const activeSortKey = () => {
    const active = document.querySelector("[data-sort-button][aria-pressed='true']");
    return active ? active.getAttribute("data-sort-key") || "wilson" : "wilson";
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

  const initCardFilter = () => {
    document.querySelectorAll("[data-card-filter-form]").forEach((form) => {
      if (form.dataset.cardFilterReady === "1") {
        return;
      }
      form.dataset.cardFilterReady = "1";
      const input = form.querySelector("[data-card-filter-input]");
      if (!(input instanceof HTMLInputElement)) {
        return;
      }
      const originalValue = input.value.trim();
      let submitTimer = null;
      const submitWithCurrentCard = () => {
        if (input.value.trim() === originalValue) {
          return;
        }
        if (typeof form.requestSubmit === "function") {
          form.requestSubmit();
        } else {
          form.submit();
        }
      };
      input.addEventListener("input", () => {
        window.clearTimeout(submitTimer);
        // 用户停顿后自动刷新服务端筛选，确保未加载的榜单条目也能被搜到。
        submitTimer = window.setTimeout(submitWithCurrentCard, 650);
      });
    });
  };

  initVariantViewers();
  initSortToolbars();
  initLoadMore();
  initCardFilter();
})();
