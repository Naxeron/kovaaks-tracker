    def _rebuild_data(self):
        """Build unified row list from current data and update the UI."""
        scenario_info = self._scenario_info
        user_by_lid = self._user_by_lid
        friends_by_lid = self._friends_by_lid
        rows = []
        played = 0
        unplayed = 0
        self._global_points_sum = 0
        self._global_potential_points_sum = 0
        self._global_projected_gain_sum = 0

        aim_type_pcts = {}
        for lid, info in scenario_info.items():
            if (u_data := user_by_lid.get(lid)) and (entries := safe_int(info.get("entries", 0))) > 0:
                if (rank := safe_int(u_data.get("rank"))) is not None and (aim_type := info.get("aimType")):
                    aim_type_pcts.setdefault(aim_type, []).append((1 - rank / entries) * 100)

        aim_type_avgs = {atype: sum(pcts) / len(pcts) for atype, pcts in aim_type_pcts.items()}
        all_pcts = [p for pcts in aim_type_pcts.values() for p in pcts]
        global_avg_pct = sum(all_pcts) / len(all_pcts) if all_pcts else 50.0
        self._aim_type_avgs = aim_type_avgs

        stats_dir = self._get_stats_dir()
        # Use cached local stats unless marked dirty
        if self._local_stats_dirty:
            self._local_stats_cache = _get_local_stats(stats_dir)
            self._local_stats_dirty = False
        local_stats = self._local_stats_cache
        now = datetime.datetime.now()
        entry_history = self._scores_cache.get("entry_history", {})

        SCENARIO_BLACKLIST = {
        }
        
        show_hidden = self._filters.get("hidden").get() if "hidden" in self._filters else False

        for lid, info in scenario_info.items():
            sname = info["name"]
            if sname in SCENARIO_BLACKLIST:
                continue
                
            is_hidden = lid in self._hidden_scenarios
            if show_hidden and not is_hidden:
                continue
            if not show_hidden and is_hidden:
                continue

            has_user = lid in user_by_lid
            has_friends = lid in friends_by_lid
            lstats = local_stats.get(sname, {"count": 0, "last_played": None, "trend": 1.0})

            hist = entry_history.get(lid, {})
            popularity_trend = 0.0
            actual_new_entries = 0
            if hist:
                dates = sorted(hist.keys())
                if len(dates) >= 2:
                    try:
                        d0_str = dates[0] if len(dates[0]) > 10 else dates[0] + "T00:00:00"
                        d0_str = d0_str.replace("Z", "+00:00") if "Z" in d0_str else d0_str
                        oldest = datetime.datetime.fromisoformat(d0_str).replace(tzinfo=None)
                        
                        d1_str = dates[-1] if len(dates[-1]) > 10 else dates[-1] + "T00:00:00"
                        d1_str = d1_str.replace("Z", "+00:00") if "Z" in d1_str else d1_str
                        newest = datetime.datetime.fromisoformat(d1_str).replace(tzinfo=None)
                        
                        seconds_diff = (newest - oldest).total_seconds()
                        if seconds_diff >= 1800:  # Need at least 30 minutes
                            # 1. Full history for stability in Trend Mult / Potential
                            days_diff = seconds_diff / 86400.0
                            entry_diff_total = hist[dates[-1]] - hist[dates[0]]
                            popularity_trend = float(entry_diff_total) / days_diff
                            
                            # 2. 24h history for "New Entries (24h)" display
                            target_24h = newest - datetime.timedelta(days=1)
                            idx_24h = 0
                            for i in range(len(dates) - 1, -1, -1):
                                ds = dates[i].replace("Z", "+00:00") if "Z" in dates[i] else dates[i]
                                if len(ds) <= 10: ds += "T00:00:00"
                                dt = datetime.datetime.fromisoformat(ds).replace(tzinfo=None)
                                if dt <= target_24h:
                                    idx_24h = i
                                    break
                            actual_new_entries = hist[dates[-1]] - hist[dates[idx_24h]]
                    except ValueError:
                        pass

            competition_multiplier = max(0.2, math.log10(max(1.0, popularity_trend + 1.0)) / 2.0)

            row = {
                "Scenario": sname,
                "Entry Count": str(info["entries"]),
                "New Entries (24h)": str(actual_new_entries) if actual_new_entries > 0 else "0",
                "Trend Mult": f"{competition_multiplier:.2f}x",
                "Local Runs": str(lstats["count"]),
                "Potential": "",
            }

            try:
                e_val = int(info["entries"])
                if has_user:
                    r_val = int(user_by_lid[lid]["rank"])
                    self._global_points_sum += (e_val - r_val)
                    self._global_potential_points_sum += (r_val - 1)
                    
                    expected_pct = aim_type_avgs.get(info.get("aimType"), global_avg_pct)
                    expected_rank = max(1, int(e_val * (1.0 - expected_pct / 100.0)))
                    if r_val > expected_rank:
                        gain = r_val - expected_rank
                        self._global_projected_gain_sum += gain
                        row["_projected_gain"] = gain
                else:
                    self._global_potential_points_sum += (e_val - 1)
                    
                    expected_pct = aim_type_avgs.get(info.get("aimType"), global_avg_pct)
                    expected_rank = max(1, int(e_val * (1.0 - expected_pct / 100.0)))
                    gain = e_val - expected_rank
                    self._global_projected_gain_sum += gain
                    row["_projected_gain"] = gain
            except (ValueError, TypeError):
                pass

            if has_user or has_friends:
                played += 1
                best = None
                if has_friends:
                    for fr in friends_by_lid[lid]:
                        try:
                            frank = int(fr["rank"])
                        except (ValueError, TypeError):
                            frank = 999999
                        if best is None or frank < best[1]:
                            best = (fr["friend"], frank, fr["score"], fr.get("date", ""))

                row["My Rank"] = str(user_by_lid[lid]["rank"]) if has_user else ""
                row["My Score"] = str(user_by_lid[lid]["score"]) if has_user else ""
                row["Score Date"] = user_by_lid[lid].get("date", "") if has_user else ""
                row["Top Friend"] = best[0] if best else ""
                row["Friend Rank"] = str(best[1]) if best else ""
                row["Friend Score"] = str(best[2]) if best else ""
                row["Friend Score Date"] = best[3] if best else ""

                if has_user and row["My Rank"] and row["Entry Count"]:
                    try:
                        rank = int(row["My Rank"])
                        entries = int(row["Entry Count"])
                        pct = (1 - rank / entries) * 100
                        row["Percentile"] = f"{pct:.2f}%"

                        # Calculate Potential Score (Optimized Algorithm)
                        # 1. Logarithmic Potential — neutralizes population bias
                        skill_gap = 1.0 - pct / 100.0
                        log_weight = math.log10(max(rank, 10))
                        base_potential = log_weight * skill_gap

                        # 2. Spaced Repetition (Time Factor) — Ebbinghaus curve
                        if lstats.get("last_played"):
                            days_ago = (now - lstats["last_played"]).total_seconds() / 86400.0
                            time_factor = 0.8 + 0.7 * (1.0 - math.exp(-max(0, days_ago) / 14.0))
                        else:
                            time_factor = 1.5  # Maximum priority for unplayed benchmarks

                        # 3. Session Fatigue — decoupled from PB tracking (fixes min() bug)
                        runs_today = lstats.get("runs_today", 0)
                        fatigue_factor = math.exp(-runs_today / 12.0)

                        # 4. Variance-Modulated Plateau Penalty (Sigmoid Decay)
                        pb_ago = lstats.get("runs_since_recent_pb", 0)
                        trend = lstats.get("trend", 1.0)
                        if trend <= 1.02:
                            # Sigmoid: max 85% penalty, inflection at 20 runs
                            plateau_penalty = 1.0 - (0.85 / (1.0 + math.exp(-0.4 * (pb_ago - 20.0))))
                        else:
                            plateau_penalty = 1.0

                        # 5. Active Learning Bonus — clamped trend factor
                        trend_factor = max(0.8, min(trend, 1.3))

                        # 6. Final Potential — *1000 converts small log floats to readable ints
                        potential = (base_potential * 1000) * time_factor * fatigue_factor * plateau_penalty * trend_factor * competition_multiplier
                        row["Potential"] = f"{int(potential)}"
                        # (Removed global summation of formula-based potential)

                    except (ValueError, TypeError, ZeroDivisionError):
                        row["Percentile"] = ""
                else:
                    row["Percentile"] = ""

                if best and row["Entry Count"]:
                    try:
                        fpct = (1 - best[1] / int(row["Entry Count"])) * 100
                        row["Friend Percentile"] = f"{fpct:.2f}%"
                    except (ValueError, TypeError, ZeroDivisionError):
                        row["Friend Percentile"] = ""
                else:
                    row["Friend Percentile"] = ""

                rank_diff = ""
                if has_user and best:
                    try:
                        rank_diff = str(int(row["My Rank"]) - best[1])
                    except (ValueError, TypeError):
                        pass
                row["Rank Diff"] = rank_diff

                pctile_diff = ""
                if row["Percentile"] and row["Friend Percentile"]:
                    try:
                        my_pct = float(row["Percentile"].rstrip("%"))
                        fr_pct = float(row["Friend Percentile"].rstrip("%"))
                        pctile_diff = f"{my_pct - fr_pct:+.2f}%"
                    except (ValueError, TypeError):
                        pass
                row["Pctile Diff"] = pctile_diff
            else:
                unplayed += 1
                row["My Rank"] = ""
                row["My Score"] = ""
                row["Percentile"] = ""
                row["Score Date"] = ""
                row["Top Friend"] = ""
                row["Friend Rank"] = ""
                row["Friend Score"] = ""
                row["Friend Percentile"] = ""
                row["Friend Score Date"] = ""
                row["Rank Diff"] = ""
                row["Pctile Diff"] = ""

            rows.append(row)

        self._all_data = rows
        self._apply_current_sort()
        self.after(0, lambda: self._apply_filter())

        if "Mock" not in type(self).__name__:
            if self._next_global_points is not None:
                if self._global_points_sum >= self._next_global_points:
                    self._next_global_points = None
                    self._next_rank_var.set("Next Rank: Loading...")
                    self._unplayed_needed_var.set("Unplayed Needed: Loading...")
                    if not self._fetching_next_rank:
                        self._fetching_next_rank = True
                        threading.Thread(target=self._fetch_next_rank_points, daemon=True).start()
                else:
                    self._update_next_rank_display()
            else:
                self._next_rank_var.set("Next Rank: Loading...")
                self._unplayed_needed_var.set("Unplayed Needed: Loading...")
                if not self._fetching_next_rank:
                    self._fetching_next_rank = True
                    threading.Thread(target=self._fetch_next_rank_points, daemon=True).start()
