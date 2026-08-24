#if UNITY_EDITOR
using System;
using System.Collections.Generic;
using System.Text;
using UnityEngine;
using UnityEditor;
using UnityEngine.Networking;

namespace Quartermaster
{
    /// <summary>
    /// VaultMCP in-editor window. Search your entire asset vault (Unity + Fab,
    /// cloud + downloaded) without leaving Unity, and import downloaded
    /// packages straight into this project.
    ///
    /// Requires the VaultMCP server running (python -m src.desktop or src.server).
    /// </summary>
    public class QuartermasterWindow : EditorWindow
    {
        private const string BaseUrl = "http://localhost:7890";
        private static string _authToken = "";

        private string _search = "";
        private string _status = "Ready.";
        private Vector2 _scroll;
        private bool _stripDemos = true;

        // result list
        private readonly List<QuartermasterAsset> _results = new List<QuartermasterAsset>();
        private int _selected = -1;
        private QuartermasterAsset _detail;
        private string _engineFilter = "all";
        private string _categoryFilter = "all";
        private List<string> _categories = new List<string>();

        [Serializable] private class QuartermasterAsset
        {
            public string id;
            public string source;      // unity | fab
            public string title;
            public string publisher;
            public string category;
            public string version;
            public string size_str;
            public string local_path;
            public string summary;
            public string usage_notes;
            public string store_url;
        }

        [MenuItem("Window/Quartermaster")]
        public static void Open()
        {
            var w = GetWindow<QuartermasterWindow>("Quartermaster");
            w.minSize = new Vector2(420, 480);
        }

        
        private static string GetAuthToken()
        {
            if (!string.IsNullOrEmpty(_authToken)) return _authToken;
            
            // 1. Check EditorPrefs
            string saved = EditorPrefs.GetString("Quartermaster_AuthToken", "");
            if (!string.IsNullOrEmpty(saved)) { _authToken = saved; return _authToken; }

            // 2. Check ~/.quartermaster/auth_token or legacy ~/.vaultmcp/auth_token
            try
            {
                string home = Environment.GetFolderPath(Environment.SpecialFolder.UserProfile);
                string path = System.IO.Path.Combine(home, ".quartermaster", "auth_token");
                if (!System.IO.File.Exists(path))
                    path = System.IO.Path.Combine(home, ".vaultmcp", "auth_token");

                if (System.IO.File.Exists(path))
                {
                    _authToken = System.IO.File.ReadAllText(path).Trim();
                    return _authToken;
                }
            }
            catch {}

            // 3. Check %LOCALAPPDATA%/Quartermaster/token or legacy %LOCALAPPDATA%/VaultMCP/token
            try
            {
                string localApp = Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
                string path = System.IO.Path.Combine(localApp, "Quartermaster", "token");
                if (!System.IO.File.Exists(path))
                    path = System.IO.Path.Combine(localApp, "VaultMCP", "token");

                if (System.IO.File.Exists(path))
                {
                    _authToken = System.IO.File.ReadAllText(path).Trim();
                    return _authToken;
                }
            }
            catch {}

            return "";
        }

        private static void SetAuthHeaders(UnityWebRequest req)
        {
            string tok = GetAuthToken();
            if (!string.IsNullOrEmpty(tok))
            {
                req.SetRequestHeader("X-Quartermaster-Token", tok);
                req.SetRequestHeader("X-VaultMCP-Token", tok);
                req.SetRequestHeader("Authorization", "Bearer " + tok);
            }
        }

        private void OnEnable()
        {
            FetchCategories();
            if (_results.Count == 0) DoSearch();
        }

        // ---------------- API helpers ----------------

        [Serializable] private class SearchResponse { public QuartermasterAsset[] items; }
        [Serializable] private class CategoriesResponse { public string[] categories; }
        [Serializable] private class ImportResponse
        {
            public string status;
            public string title;
            public int written;
            public int stripped;
            public double stripped_mb;
            public string[] prefabs;
            public string[] warnings;
        }

        private T GetJson<T>(string url) where T : class
        {
            using (var req = UnityWebRequest.Get(url))
            {
                req.timeout = 15;
                SetAuthHeaders(req);
                var op = req.SendWebRequest();
                while (!op.isDone) { System.Threading.Thread.Sleep(10); }
#if UNITY_2020_1_OR_NEWER
                if (req.result != UnityWebRequest.Result.Success) { _status = "HTTP error: " + req.error; return null; }
#else
                if (req.isNetworkError || req.isHttpError) { _status = "HTTP error: " + req.error; return null; }
#endif
                return JsonUtility.FromJson<T>(req.downloadHandler.text);
            }
        }

        private T PostJson<T>(string url, string json) where T : class
        {
            using (var req = new UnityWebRequest(url, "POST"))
            {
                byte[] body = Encoding.UTF8.GetBytes(json);
                req.uploadHandler = new UploadHandlerRaw(body);
                req.downloadHandler = new DownloadHandlerBuffer();
                req.SetRequestHeader("Content-Type", "application/x-www-form-urlencoded");
                SetAuthHeaders(req);
                req.timeout = 600;
                var op = req.SendWebRequest();
                bool cancelled = false;
                while (!op.isDone && !cancelled)
                {
                    System.Threading.Thread.Sleep(50);
                    cancelled = EditorUtility.DisplayCancelableProgressBar(
                        "Quartermaster", "Importing package…", Mathf.Clamp01(op.progress));
                }
                EditorUtility.ClearProgressBar();
#if UNITY_2020_1_OR_NEWER
                if (req.result != UnityWebRequest.Result.Success)
#else
                if (req.isNetworkError || req.isHttpError)
#endif
                {
                    _status = "Import failed: " + req.error + "\n" + req.downloadHandler.text;
                    return null;
                }
                if (cancelled) { _status = "Import cancelled."; return null; }
                return JsonUtility.FromJson<T>(req.downloadHandler.text);
            }
        }

        private void FetchCategories()
        {
            var resp = GetJson<CategoriesResponse>(BaseUrl + "/api/categories");
            if (resp?.categories != null) { _categories = new List<string>(resp.categories); }
        }

        private void DoSearch()
        {
            var url = BaseUrl + "/api/assets?query=" + WWW.EscapeURL(_search) +
                      "&source=" + _engineFilter +
                      "&category=" + WWW.EscapeURL(_categoryFilter) +
                      "&limit=200";
            var resp = GetJson<SearchResponse>(url);
            _results.Clear(); _selected = -1; _detail = null;
            if (resp?.items != null) _results.AddRange(resp.items);
            _status = _results.Count + " results.";
            Repaint();
        }

        private void DoImport(QuartermasterAsset asset)
        {
            string projectRoot = Directory.GetParent(Application.dataPath).FullName;
            string body = "asset_id=" + WWW.EscapeURL(asset.id) +
                          "&project_dir=" + WWW.EscapeURL(projectRoot) +
                          "&strip_demos=" + (_stripDemos ? "true" : "false");
            var resp = PostJson<ImportResponse>(BaseUrl + "/api/import", body);
            if (resp == null) { Repaint(); return; }

            if (!string.IsNullOrEmpty(resp.status) && resp.status == "ok")
            {
                AssetDatabase.Refresh(ImportAssetOptions.Default);
                _status = $"Imported '{resp.title}': {resp.written} files" +
                          (resp.stripped > 0 ? $", stripped {resp.stripped} demo/doc files ({resp.stripped_mb} MB)" : "");
                if (resp.warnings != null && resp.warnings.Length > 0)
                    _status += "\n⚠ " + string.Join("\n⚠ ", resp.warnings);

                // offer to instantiate the first matching prefab
                if (resp.prefabs != null && resp.prefabs.Length > 0 &&
                    EditorUtility.DisplayDialog("Quartermaster",
                        $"Imported {resp.written} files.\nAdd a prefab to the scene?\n\n{resp.prefabs[0]}",
                        "Add to Scene", "Skip"))
                {
                    string rel = "Assets" + resp.prefabs[0].Replace('\\', '/')
                        .Substring(Application.dataPath.Length);
                    var prefab = AssetDatabase.LoadAssetAtPath<GameObject>(rel);
                    if (prefab != null)
                    {
                        var go = (GameObject)PrefabUtility.InstantiatePrefab(prefab);
                        Selection.activeGameObject = go;
                        SceneView.lastActiveSceneView?.FrameSelected();
                    }
                    else _status = "Prefab found but could not load: " + rel;
                }
            }
            else
            {
                // PostJson already wrote the error into _status
            }
            Repaint();
        }

        // ---------------- UI ----------------

        private void OnGUI()
        {
            // top bar
            EditorGUILayout.BeginHorizontal();
            _search = EditorGUILayout.TextField(_search);
            if (GUILayout.Button("Search", GUILayout.Width(60))) DoSearch();
            EditorGUILayout.EndHorizontal();

            EditorGUILayout.BeginHorizontal();
            _engineFilter = EditorGUILayout.Popup(_engineFilter,
                new[] { "all", "unity", "fab" }, GUILayout.Width(80));
            var catList = new List<string> { "all" };
            catList.AddRange(_categories);
            int idx = Mathf.Max(0, catList.IndexOf(_categoryFilter));
            idx = EditorGUILayout.Popup(idx, catList.ToArray(), GUILayout.Width(160));
            _categoryFilter = catList[idx];
            _stripDemos = GUILayout.Toggle(_stripDemos, "Strip demos", GUILayout.Width(100));
            if (GUILayout.Button("⟳", GUILayout.Width(24))) { FetchCategories(); DoSearch(); }
            EditorGUILayout.EndHorizontal();

            EditorGUILayout.Space(4);

            // list
            _scroll = EditorGUILayout.BeginScrollView(_scroll);
            for (int i = 0; i < _results.Count; i++)
            {
                var a = _results[i];
                var style = i == _selected ? EditorStyles.helpBox : EditorStyles.label;
                EditorGUILayout.BeginHorizontal(GUI.skin.box);
                GUILayout.Label(a.local_path != null ? "⚡" : "☁", GUILayout.Width(18));
                EditorGUILayout.BeginVertical();
                EditorGUILayout.LabelField(a.title, EditorStyles.boldLabel);
                EditorGUILayout.LabelField($"{a.publisher} · {a.category}" +
                    (a.size_str != null ? $" · {a.size_str}" : ""), EditorStyles.miniLabel);
                EditorGUILayout.EndVertical();
                EditorGUILayout.EndHorizontal();

                var rect = GUILayoutUtility.GetLastRect();
                if (Event.current.type == EventType.MouseDown && rect.Contains(Event.current.mousePosition))
                {
                    _selected = i; _detail = a; Repaint();
                }
            }
            EditorGUILayout.EndScrollView();

            // detail + actions
            if (_detail != null)
            {
                EditorGUILayout.Space(6);
                EditorGUILayout.LabelField(_detail.title, EditorStyles.whiteLargeLabel);
                EditorGUILayout.LabelField(_detail.summary ?? "", EditorStyles.wordWrappedMiniLabel);
                EditorGUILayout.BeginHorizontal();
                using (new EditorGUI.DisabledScope(string.IsNullOrEmpty(_detail.local_path)))
                {
                    if (GUILayout.Button("📥 Import into this project"))
                        DoImport(_detail);
                }
                if (GUILayout.Button("Store ↗", GUILayout.Width(70)) && !string.IsNullOrEmpty(_detail.store_url))
                    Application.OpenURL(_detail.store_url);
                EditorGUILayout.EndHorizontal();
                if (string.IsNullOrEmpty(_detail.local_path))
                    EditorGUILayout.HelpBox("Cloud-only: download via Unity Hub / Fab first.", MessageType.Info);
            }

            EditorGUILayout.Space(4);
            EditorGUILayout.HelpBox(_status, MessageType.None);
        }
    }
}
#endif
