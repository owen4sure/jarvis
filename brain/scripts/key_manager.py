import json
import os
import fcntl
from datetime import datetime, timedelta


class KeyManager:
    """Gemini 金鑰池管理。跨程序安全：所有 read-modify-write 都在檔案鎖內，
    並在鎖內重新載入，避免多程序（proxy / gemini_client / memory / research）互相覆蓋。
    report_error 可指定「實際用的那把 key 的 index」，避免併發下冷卻到錯的 key。"""

    def __init__(self, config_path='/Users/USERNAME/Hermes_Brain/config/keys.json'):
        self.config_path = config_path
        self.lock_path = config_path + '.lock'
        self.config = self._load_config()
        self._last_index = self.config.get('current_index', 0)

    def _load_config(self):
        with open(self.config_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _lock(self):
        lf = open(self.lock_path, 'w')
        fcntl.flock(lf, fcntl.LOCK_EX)
        return lf

    def _unlock(self, lf):
        try:
            fcntl.flock(lf, fcntl.LOCK_UN)
            lf.close()
        except Exception:
            pass

    def _save_nolock(self):
        """原子寫入（已在鎖內）。"""
        tmp = self.config_path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(self.config, f, indent=4, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, self.config_path)

    def _cooldown_seconds(self):
        # 1 小時太長（誤冷卻=整池假掛）。429/暫時錯誤幾分鐘就恢復；壞 key 之後會再 error 再冷卻。
        try:
            v = int(self.config.get('cooldown_period_seconds', 120) or 120)
        except Exception:
            v = 120
        return max(30, min(v, 300))

    def _refresh_nolock(self):
        keys = self.config.get('api_keys', [])
        now = datetime.now().timestamp()
        changed = False
        for ki in keys:
            if ki.get('status') == 'error' and now >= ki.get('available_at', 0):
                ki['status'] = 'active'
                changed = True
        if changed:
            self._save_nolock()

    def get_key(self):
        """回傳目前可用的 key 字串（向後相容）。"""
        return self.get_key_with_index()[1]

    def get_key_with_index(self):
        """回傳 (index, key)。呼叫端記住 index，出錯時用 report_error(code, index) 精準冷卻。"""
        lf = self._lock()
        try:
            self.config = self._load_config()       # 看到其他程序的最新狀態
            self._refresh_nolock()
            keys = self.config.get('api_keys', [])
            if not keys:
                raise RuntimeError("config/keys.json 沒有任何 api_keys")
            current_idx = self.config.get('current_index', 0) % len(keys)
            for i in range(len(keys)):
                idx = (current_idx + i) % len(keys)
                if keys[idx].get('status') == 'active':
                    self.config['current_index'] = idx
                    self._last_index = idx
                    self._save_nolock()
                    return idx, keys[idx]['key']
            raise RuntimeError("No active API key available in config/keys.json")
        finally:
            self._unlock(lf)

    def report_error(self, error_code, index=None):
        """把「實際用的那把 key」標記 error 並冷卻。index 省略時退回 current_index（向後相容）。"""
        lf = self._lock()
        try:
            self.config = self._load_config()
            keys = self.config.get('api_keys', [])
            if not keys:
                return
            idx = self.config.get('current_index', 0) if index is None else index
            idx = idx % len(keys)
            t = datetime.now()
            keys[idx]['status'] = 'error'
            keys[idx]['last_error_at'] = t.isoformat()
            keys[idx]['available_at'] = (t + timedelta(seconds=self._cooldown_seconds())).timestamp()
            self.config['current_index'] = (idx + 1) % len(keys)
            self._save_nolock()
        finally:
            self._unlock(lf)

    def _refresh_keys(self):
        lf = self._lock()
        try:
            self.config = self._load_config()
            self._refresh_nolock()
        finally:
            self._unlock(lf)


if __name__ == "__main__":
    manager = KeyManager()
    print("測試取得 Key:", manager.get_key_with_index()[0])
