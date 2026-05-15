import csv
import hashlib
import logging
import random
import threading
import time
from datetime import datetime
from io import StringIO
from pathlib import Path

import archive as archive_mod
import config as cfg_mod
import lm_client
import stats as stats_mod
import validator

log = logging.getLogger(__name__)
BASE = Path(__file__).parent


class Orchestrator:
    def __init__(self, broadcast_fn):
        self._broadcast = broadcast_fn
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._skip = threading.Event()
        self._lm_unavailable = False
        self._stock_deck: list[dict] = []
        self._replay_deck: list[Path] = []
        self.state = "IDLE"
        self.current_file: Path | None = None
        self.last_state_msg: dict = {"type": "state", "state": "IDLE"}

    # ── Control ────────────────────────────────────────────────────────────────

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name="orchestrator")
        self._thread.start()
        log.info("Orchestrator started")

    def stop(self) -> None:
        self._stop.set()

    def skip(self) -> None:
        self._skip.set()

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _tx(self, state: str, **kw) -> None:
        self.state = state
        msg = {"type": "state", "state": state, **kw}
        self.last_state_msg = msg
        log.info("STATE → %s %s", state, kw or "")
        self._broadcast(msg)

    def _push_token(self, tok: str) -> None:
        self._broadcast({"type": "token", "token": tok})

    def _push_status(self, text: str) -> None:
        self._broadcast({"type": "status", "text": text})

    # ── Main loop ──────────────────────────────────────────────────────────────

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                cfg = cfg_mod.load()
                if cfg["boot_mode"] == "replay":
                    self._replay_cycle(cfg)
                elif cfg.get("lm_fallback_to_replay", True):
                    self._probe_lm(cfg)
                    if self._lm_unavailable:
                        self._push_status("LM Studio unreachable — replaying archive")
                        self._replay_cycle(cfg)
                    else:
                        self._generate_cycle(cfg)
                else:
                    self._generate_cycle(cfg)
            except Exception:
                log.exception("Unhandled error in orchestrator")
                time.sleep(10)

    # ── Replay ─────────────────────────────────────────────────────────────────

    def _replay_cycle(self, cfg: dict) -> None:
        if not self._replay_deck:
            files = archive_mod.list_files(cfg)
            if not files:
                log.info("Archive empty; sleeping")
                self._push_status("ARCHIVE EMPTY — waiting for demos")
                time.sleep(15)
                return
            self._replay_deck = files.copy()
            random.shuffle(self._replay_deck)
            log.info("Replay deck refilled: %d files", len(self._replay_deck))
        path = None
        while self._replay_deck:
            candidate = self._replay_deck.pop()
            if candidate.exists():
                path = candidate
                break
        if path is None:
            return  # all candidates were deleted; next cycle refills
        entry = archive_mod.get_meta(path.name, cfg)
        entry_mode   = entry.get("mode", "")
        entry_effect = entry.get("effect", "")
        entry_model  = entry.get("model", "")
        stats_key = entry_effect if entry_mode == "stock" and entry_effect else entry_mode or "unknown"
        meta = {
            "effect":      entry_effect,
            "title":       entry.get("title", ""),
            "description": entry.get("description", ""),
            "model":       entry_model,
        }
        runtime = cfg["display"]["demo_runtime_seconds"]

        # JIT titling: generate title synchronously before displaying
        if cfg["display"].get("jit_titling") and not meta["title"] and not self._lm_unavailable:
            try:
                available = set(lm_client.list_models(cfg["lm_studio"]["base_url"]))
            except Exception:
                available = set()
            use_model = entry_model if entry_model in available else ""
            result = self._generate_title_meta(
                cfg, path.read_text(encoding="utf-8"), model=use_model
            )
            if result:
                meta.update(result)
                archive_mod.update_meta(path.name, result, cfg)

        stats_summary = self._meta_stats(stats_key, entry_model)
        meta["stats"] = stats_summary
        self._tx("DISPLAY", url=f"/archive/{path.name}", runtime=runtime, meta=meta)

        # Background enrichment only if JIT is off and title still missing
        if not cfg["display"].get("jit_titling") and not meta["title"] and not self._lm_unavailable:
            _path = path
            _entry_model = entry_model
            def _enrich():
                try:
                    html = _path.read_text(encoding="utf-8")
                    result = self._generate_title_meta(cfg_mod.load(), html, model=_entry_model)
                    if result:
                        archive_mod.update_meta(_path.name, result, cfg_mod.load())
                        log.info("Enriched title for %s", _path.name)
                except Exception:
                    log.exception("Title enrichment failed for %s", _path.name)
            threading.Thread(target=_enrich, daemon=True).start()
        self._run_timer(runtime)

    def _probe_lm(self, cfg: dict) -> None:
        try:
            lm_client.list_models(cfg["lm_studio"]["base_url"])
            if self._lm_unavailable:
                log.info("LM Studio reachable again — resuming generate mode")
                self._push_status("LM Studio reconnected — resuming generate mode")
            self._lm_unavailable = False
        except lm_client.LMClientError:
            self._lm_unavailable = True

    # ── Generate cycle ─────────────────────────────────────────────────────────

    def _generate_cycle(self, cfg: dict) -> None:
        self._skip.clear()

        # PICK MODE
        self._tx("PICK_MODE")
        mode, prompt_data = self._pick_mode(cfg)
        if mode is None:
            time.sleep(5)
            return

        # Resolve model (surprise me picks at random)
        lm_cfg = cfg["lm_studio"]
        model = lm_cfg["model"]
        if lm_cfg.get("surprise_me"):
            try:
                available = lm_client.list_models(lm_cfg["base_url"])
                if available:
                    model = random.choice(available)
                    self._broadcast({"type": "model_selected", "model": model})
                    log.info("Surprise Me selected model: %s", model)
            except lm_client.LMClientError:
                pass  # fall back to configured model

        # GENERATE
        self._tx("GENERATE", mode=mode)
        html = self._do_generate(cfg, mode, prompt_data, model=model)
        if html is None:
            self._tx("DISCARD")
            return

        # Save to temp for audition
        temp_path = self._save_temp(html)

        # VALIDATE + REPAIR loop
        max_repairs = cfg["validation"]["max_repair_attempts"]
        audition_secs = cfg["validation"]["audition_seconds"]
        last_error: dict = {"kind": None, "msg": None}

        for attempt in range(max_repairs + 1):
            self._tx("VALIDATE", attempt=attempt)
            result = self._audition(temp_path, audition_secs)

            if result.get("result") == "OK":
                last_error = {"kind": None, "msg": None}
                break

            last_error = {
                "kind": result.get("result", "CRASHED"),
                "msg": result.get("error", ""),
            }

            if attempt >= max_repairs:
                log.info("Max repairs reached; discarding")
                if last_error["kind"]:
                    if mode == "stock" and isinstance(prompt_data, dict):
                        stats_mod.record_failure(
                            prompt_data.get("effect", "unknown"), last_error["kind"], model=model
                        )
                    elif mode in ("creative", "update"):
                        stats_mod.record_failure(mode, last_error["kind"], model=model)
                self._tx("DISCARD", kind=last_error["kind"], error=last_error["msg"])
                temp_path.unlink(missing_ok=True)
                return

            error_msg = last_error["msg"] or "unknown error"
            if cfg["display"].get("show_retry_messages", True):
                self._push_status(
                    f"ERROR DETECTED — REQUESTING REPAIR — ATTEMPT {attempt + 1}/{max_repairs}"
                )

            self._tx("REPAIR", attempt=attempt + 1, max=max_repairs)
            html = self._do_repair(cfg, html, error_msg, model=model)
            if html is None:
                self._tx("DISCARD")
                temp_path.unlink(missing_ok=True)
                return
            temp_path.write_text(html, encoding="utf-8")

        # DISPLAY
        runtime = cfg["display"]["demo_runtime_seconds"]
        source_path = prompt_data.get("source_path") if isinstance(prompt_data, dict) else None
        effect_slug = prompt_data.get("effect", mode) if isinstance(prompt_data, dict) else mode
        stats_key = (prompt_data.get("effect", "unknown")
                     if mode == "stock" and isinstance(prompt_data, dict) else mode)
        stats_summary = self._meta_stats(stats_key, model)

        if cfg["display"].get("jit_titling"):
            jit_result = self._generate_title_meta(cfg, html, model=model)
            display_meta = {
                "effect":      effect_slug,
                "model":       model,
                "title":       jit_result.get("title", "") if jit_result else "",
                "description": jit_result.get("description", "") if jit_result else "",
                "stats":       stats_summary,
            }
            self._tx("DISPLAY", url=f"/temp/{temp_path.name}", runtime=runtime, meta=display_meta)
            self._run_timer(runtime)

            self._tx("ARCHIVE")
            archive_path = archive_mod.save(html, effect_slug, mode, cfg,
                                            source_path=source_path, model=model)
            temp_path.unlink(missing_ok=True)
            if jit_result:
                archive_mod.update_meta(archive_path.name, jit_result, cfg)
        else:
            display_meta = {
                "effect":      effect_slug,
                "model":       model,
                "title":       "",
                "description": "",
                "stats":       stats_summary,
            }
            self._tx("DISPLAY", url=f"/temp/{temp_path.name}", runtime=runtime, meta=display_meta)

            # Background title gen while demo plays
            _html_for_meta = html
            _model_for_meta = model
            _meta_result: list = [None]
            def _fetch_meta():
                _meta_result[0] = self._generate_title_meta(cfg, _html_for_meta,
                                                             model=_model_for_meta)
            meta_thread = threading.Thread(target=_fetch_meta, daemon=True)
            meta_thread.start()

            self._run_timer(runtime)

            self._tx("ARCHIVE")
            archive_path = archive_mod.save(html, effect_slug, mode, cfg,
                                            source_path=source_path, model=model)
            temp_path.unlink(missing_ok=True)
            meta_thread.join(timeout=10)
            if _meta_result[0]:
                archive_mod.update_meta(archive_path.name, _meta_result[0], cfg)

        # Record successful run in stats
        if mode == "stock" and isinstance(prompt_data, dict):
            stats_mod.record_run(prompt_data.get("effect", "unknown"), model=model)
        elif mode in ("creative", "update"):
            stats_mod.record_run(mode, model=model)

        self._tx("IDLE")

    def _run_timer(self, seconds: float) -> None:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop.is_set() or self._skip.is_set():
                break
            time.sleep(0.25)
        self._skip.clear()

    # ── Mode selection ─────────────────────────────────────────────────────────

    def _pick_mode(self, cfg: dict) -> tuple[str | None, dict | None]:
        weights = cfg["generate_mode_weights"]
        pool = [(m, w) for m, w in weights.items() if w > 0]
        if not pool:
            log.warning("All mode weights are 0")
            return None, None

        total = sum(w for _, w in pool)
        r = random.uniform(0, total)
        cumulative = 0.0
        chosen = pool[-1][0]
        for m, w in pool:
            cumulative += w
            if r <= cumulative:
                chosen = m
                break

        if chosen == "update":
            lm = cfg["lm_studio"]
            max_input = lm["context_ceiling_tokens"] - lm["max_tokens"]
            src = archive_mod.pick_for_update(cfg, max_input)
            if src is None:
                log.info("No archive files for update; falling back to stock")
                chosen = "stock"
            else:
                return "update", {"effect": "update", "source_path": src}

        if chosen == "stock":
            row = self._pick_csv_row()
            return ("stock", row) if row else (None, None)

        # creative
        return "creative", {"effect": "creative"}

    def _pick_csv_row(self) -> dict | None:
        csv_path = BASE / "prompts.csv"
        if not csv_path.exists():
            log.error("prompts.csv not found")
            return None
        if not self._stock_deck:
            try:
                with open(csv_path, "r", encoding="utf-8", newline="") as f:
                    rows = list(csv.DictReader(f))
            except Exception:
                log.exception("Failed to read prompts.csv")
                return None
            if not rows:
                return None
            self._stock_deck = rows.copy()
            random.shuffle(self._stock_deck)
            log.info("Stock deck refilled: %d prompts", len(self._stock_deck))
        row = self._stock_deck.pop()
        return {
            "effect": row.get("Effect", "unknown"),
            "prompt": row.get("Three.js prompt", ""),
        }

    def _csv_text_for_creative(self, cfg: dict) -> str:
        csv_path = BASE / "prompts.csv"
        if not csv_path.exists():
            return ""
        lm = cfg["lm_studio"]
        max_chars = (lm["context_ceiling_tokens"] - lm["max_tokens"]) * 4 - 500
        content = csv_path.read_text(encoding="utf-8")
        if len(content) <= max_chars:
            return content
        reader = csv.reader(StringIO(content))
        rows = list(reader)
        header = rows[:1]
        data = sorted(rows[1:], key=lambda r: len(",".join(r)))
        out = list(header)
        total = len(",".join(header[0])) if header else 0
        for row in data:
            line = ",".join(row)
            if total + len(line) + 1 > max_chars:
                break
            out.append(row)
            total += len(line) + 1
        log.warning("CSV truncated to %d rows for token budget", len(out) - 1)
        import io
        buf = io.StringIO()
        csv.writer(buf).writerows(out)
        return buf.getvalue()

    # ── Generation ─────────────────────────────────────────────────────────────

    def _build_messages(self, cfg: dict, mode: str, prompt_data: dict) -> list[dict]:
        sys_prompts = cfg["system_prompts"]
        if mode == "stock":
            return [
                {"role": "system", "content": sys_prompts["stock"]},
                {"role": "user", "content": prompt_data.get("prompt", "")},
            ]
        if mode == "creative":
            csv_text = self._csv_text_for_creative(cfg)
            return [
                {"role": "system", "content": sys_prompts["creative"]},
                {"role": "user", "content": f"Stock effects for inspiration:\n\n{csv_text}"},
            ]
        if mode == "update":
            src: Path = prompt_data.get("source_path")
            src_html = src.read_text(encoding="utf-8") if src and Path(src).exists() else ""
            return [
                {"role": "system", "content": sys_prompts["update"]},
                {"role": "user", "content": f"Existing demo to update:\n\n{src_html}"},
            ]
        return []

    def _stream(self, cfg: dict, messages: list[dict], model: str = "") -> tuple[str | None, str | None]:
        """Run the LLM stream; return (html, error_msg)."""
        lm = cfg["lm_studio"]
        tokens: list[str] = []
        start = time.time()

        try:
            for tok in lm_client.chat_stream(
                messages=messages,
                base_url=lm["base_url"],
                model=model or lm["model"],
                max_tokens=lm["max_tokens"],
                temperature=lm["temperature"],
                timeout=lm["request_timeout_seconds"],
            ):
                if self._skip.is_set():
                    log.info("Aborting LLM stream: skip requested")
                    return None, None
                tokens.append(tok)
                self._push_token(tok)
        except lm_client.LengthFinishError:
            log.warning("LLM hit token limit")
            if not tokens:
                return None, "LLM produced no output before token limit"
            # Fall through; repair will fix the truncated output
            html = validator.strip_fences("".join(tokens))
            return html, "output was truncated at token limit"
        except lm_client.LMClientError as e:
            log.error("LM Studio unreachable: %s", e)
            self._lm_unavailable = True
            return None, None

        # Wait for display to drain the typing queue
        total_chars = sum(len(t) for t in tokens)
        display_rate = cfg["display"].get("display_chars_per_sec", 400)
        min_typing = cfg["display"].get("min_typing_seconds", 8)
        elapsed = time.time() - start
        drain_time = total_chars / display_rate
        wait = max(0.0, min_typing - elapsed, drain_time - elapsed)
        if wait > 0:
            time.sleep(wait)

        return validator.strip_fences("".join(tokens)), None

    def _do_generate(self, cfg: dict, mode: str, prompt_data: dict, model: str = "") -> str | None:
        messages = self._build_messages(cfg, mode, prompt_data)
        html, err = self._stream(cfg, messages, model=model)
        if err and html:
            log.warning("Generation had error: %s", err)
        return html

    def _do_repair(self, cfg: dict, html: str, error: str, model: str = "") -> str | None:
        repair_prompt = cfg["system_prompts"]["repair"].format(error=error)
        messages = [
            {"role": "system", "content": repair_prompt},
            {"role": "user", "content": html},
        ]
        repaired, _ = self._stream(cfg, messages, model=model)
        return repaired

    # ── Title / description generation ────────────────────────────────────────

    def _meta_stats(self, effect_key: str, model: str) -> dict:
        raw = stats_mod.load()
        def _row(entry):
            runs  = entry.get("runs", 0)
            fails = entry.get("total", 0)
            return {
                "runs": runs, "fails": fails,
                "deletions": entry.get("deletions", 0),
                "fail_pct": round(fails / runs * 100, 1) if runs else None,
            }
        eff_entry = raw.get(effect_key, {})
        mdl_entry = eff_entry.get("by_model", {}).get(model, {}) if model else {}
        return {"effect": _row(eff_entry), "model": _row(mdl_entry)}

    def _generate_title_meta(self, cfg: dict, html: str, model: str = "") -> dict | None:
        import json as _json
        prompt = cfg["system_prompts"].get("title", "")
        if not prompt:
            return None
        lm = cfg["lm_studio"]
        use_model = model or lm["model"]
        messages = [
            {"role": "system", "content": prompt},
            {"role": "user", "content": html[:6000]},
        ]
        try:
            text = lm_client.chat(
                messages=messages,
                base_url=lm["base_url"],
                model=use_model,
                max_tokens=128,
                temperature=0.7,
                timeout=30,
            )
            # Extract JSON object even if the model wrapped it in markdown fences
            start = text.find('{')
            end   = text.rfind('}')
            if start != -1 and end != -1:
                text = text[start:end + 1]
            data = _json.loads(text)
            return {
                "title": str(data.get("title", "")),
                "description": str(data.get("description", "")),
            }
        except Exception as e:
            log.warning("Title generation failed: %s", e)
            return None

    # ── Temp file & audition ───────────────────────────────────────────────────

    def _save_temp(self, html: str) -> Path:
        temp_dir = BASE / "temp"
        temp_dir.mkdir(exist_ok=True)
        ts = datetime.now().strftime("%H%M%S")
        h = hashlib.md5(html.encode()).hexdigest()[:6]
        path = temp_dir / f"demo_{ts}_{h}.html"
        path.write_text(html, encoding="utf-8")
        return path

    def _audition(self, path: Path, seconds: float) -> dict:
        validator.start_validation()
        self._broadcast({
            "type": "audition",
            "url": f"/temp/{path.name}",
            "seconds": seconds,
        })
        result = validator.wait_for_result(timeout=seconds + 15)
        if result is None:
            log.warning("Audition timed out — treating as OK")
            return {"result": "OK"}
        return result
