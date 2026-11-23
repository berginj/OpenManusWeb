// connected_interface.js - 主要JavaScript文件，负责初始化和协调其他模块

// 导入各个管理器类
import { WebSocketManager } from '/static/connected_websocketManager.js';
import { ChatManager } from '/static/connected_chatManager.js';
import { ThinkingManager } from '/static/connected_thinkingManager.js';
import { WorkspaceManager } from '/static/connected_workspaceManager.js';
import { FileViewerManager } from '/static/connected_fileViewerManager.js';
import { initLanguage, setLanguage, updatePageTexts, t } from '/static/i18n.js';

// 主应用类
class App {
    constructor() {
        this.sessionId = null;
        this.sessionWorkspace = null;
        this.isProcessing = false;

        // 初始化各个管理器
        this.websocketManager = new WebSocketManager(this.handleWebSocketMessage.bind(this));
        this.chatManager = new ChatManager(this.handleSendMessage.bind(this));
        this.thinkingManager = new ThinkingManager();
        this.workspaceManager = new WorkspaceManager(this.handleFileClick.bind(this));
        this.fileViewerManager = new FileViewerManager();

        // 绑定UI事件
        this.bindEvents();
    }

    // 初始化应用
    init() {
        console.log('OpenManus Web应用初始化...');

        // 初始化语言设置
        const currentLang = initLanguage();
        document.getElementById('language-selector').value = currentLang;
        updatePageTexts();

        // 初始化各个管理器
        this.chatManager.init();
        this.thinkingManager.init();
        this.workspaceManager.init();
        this.fileViewerManager.init();

        // 加载工作区文件
        this.loadWorkspaceFiles();
        this.restoreSession().then(restored => { if (!restored) this.loadWorkspaceChoices(); });
    }

    async loadWorkspaceChoices(selected = null) {
        try {
            const response = await fetch('/api/workspaces', {cache: 'no-store'});
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json(), select = document.getElementById('task-workspace');
            const choice = selected ?? select.value;
            select.replaceChildren(new Option('New workspace', ''));
            for (const name of data.workspaces) select.add(new Option(name, name));
            select.value = data.workspaces.includes(choice) ? choice : '';
        } catch (error) { console.error('Could not load workspaces:', error); }
    }

    async restoreSession(requestedSid = null) {
        const sid = requestedSid || localStorage.getItem('openmanus-session-id');
        if (!sid) return false;
        if (this.isProcessing) return sid === this.sessionId;
        try {
            let response = await fetch(`/api/chat/${encodeURIComponent(sid)}`, {cache: 'no-store'});
            if (response.status === 404) response = await fetch(`/api/chat/${encodeURIComponent(sid)}/diagnostics`, {cache: 'no-store'});
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const session = await response.json();
            this.websocketManager.close();
            this.chatManager.clearMessages();
            this.thinkingManager.clearThinking();
            this.sessionId = sid;
            localStorage.setItem('openmanus-session-id', sid);
            this.sessionWorkspace = {workspace: session.workspace, project_path: session.project_path};
            if (session.project_path) {
                document.getElementById('task-workspace').value = '';
                document.getElementById('project-path').value = session.project_path;
                this.projectDirectory = '';
                await this.loadProjectFiles();
            } else {
                document.getElementById('project-path').value = '';
                document.getElementById('project-browser').hidden = true;
                if (session.workspace) {
                    await this.loadWorkspaceChoices(session.workspace);
                    if (!document.getElementById('task-workspace').value) {
                        this.chatManager.addSystemMessage('The previous workspace is no longer available. Choose a workspace for the next task.');
                    }
                }
            }
            document.getElementById('include-context').checked = true;
            if (session.context_exchanges?.length) {
                this.chatManager.addSystemMessage('Previous task excerpts included in this conversation; long excerpts may be truncated.');
                for (const exchange of session.context_exchanges) {
                    if (exchange.request) this.chatManager.addUserMessage(exchange.request);
                    if (exchange.result) this.chatManager.addAIMessage(exchange.result);
                }
            }
            if (session.prompt) this.chatManager.addUserMessage(session.prompt);
            if (session.status === 'processing') {
                this.isProcessing = true;
                document.getElementById('send-btn').disabled = true;
                document.getElementById('stop-btn').disabled = !!session.stop_requested;
                document.getElementById('status-indicator').textContent = session.stop_requested ? 'Stopping…' : t('processing_request');
                this.websocketManager.connect(sid);
            } else {
                this.isProcessing = false;
                document.getElementById('send-btn').disabled = false;
                document.getElementById('stop-btn').disabled = true;
                document.getElementById('status-indicator').textContent = session.status === 'interrupted' ? 'Interrupted; task was not resumed.' : '';
                if (session.status === 'interrupted') this.chatManager.addSystemMessage('This task was interrupted when the server stopped. The last checkpoint is shown.');
                if (session.result) this.chatManager.addAIMessage(session.result);
                const steps = session.thinking_steps || session.recent_steps;
                if (steps) this.thinkingManager.addThinkingSteps(steps);
                const logs = session.log || session.recent_session_logs || [];
                this.thinkingManager.addThinkingSteps(logs.map(entry => ({
                    message: String(entry.message ?? ''), type: 'system_log',
                    timestamp: entry.timestamp || Date.now() / 1000
                })));
            }
            return true;
        } catch (error) {
            console.error('Could not restore task:', error);
            return false;
        }
    }

    // 绑定UI事件
    bindEvents() {
        document.getElementById('task-workspace').addEventListener('change', () => {
            if (document.getElementById('task-workspace').value) document.getElementById('project-path').value = '';
        });
        document.getElementById('project-path').addEventListener('input', () => {
            if (document.getElementById('project-path').value.trim()) document.getElementById('task-workspace').value = '';
        });
        // 停止按钮
        document.getElementById('stop-btn').addEventListener('click', () => {
            if (this.sessionId && this.isProcessing) {
                this.stopProcessing();
            }
        });

        // 清除按钮
        document.getElementById('clear-btn').addEventListener('click', () => {
            this.chatManager.clearMessages();
            document.getElementById('include-context').checked = false;
        });

        // 清除思考记录按钮
        document.getElementById('clear-thinking').addEventListener('click', () => {
            this.thinkingManager.clearThinking();
        });

        // 刷新文件按钮
        document.getElementById('refresh-files').addEventListener('click', () => {
            this.loadWorkspaceFiles();
        });

        // 语言选择器
        document.getElementById('language-selector').addEventListener('change', (event) => {
            const selectedLang = event.target.value;
            setLanguage(selectedLang);
            updatePageTexts();
            this.updateDynamicTexts();
        });
    }

    // 更新动态生成的文本
    updateDynamicTexts() {
        // 更新状态指示器
        const statusIndicator = document.getElementById('status-indicator');
        if (statusIndicator.textContent.includes('正在处理')) {
            statusIndicator.textContent = t('processing_request');
        } else if (statusIndicator.textContent.includes('处理已停止')) {
            statusIndicator.textContent = t('processing_stopped');
        }

        // 更新记录计数
        const recordCount = document.getElementById('record-count');
        const count = parseInt(recordCount.textContent);
        if (!isNaN(count)) {
            recordCount.textContent = t('records_count', { count });
        }

        // 更新刷新倒计时
        const refreshCountdown = document.getElementById('refresh-countdown');
        const seconds = refreshCountdown.textContent.match(/\d+/);
        if (seconds) {
            refreshCountdown.textContent = t('refresh_countdown', { seconds: seconds[0] });
        }
    }

    // 处理发送消息
    async handleSendMessage(message) {
        if (this.isProcessing) {
            console.log('正在处理中，请等待...');
            return;
        }

        this.isProcessing = true;
        document.getElementById('send-btn').disabled = true;
        document.getElementById('stop-btn').disabled = false;
        document.getElementById('status-indicator').textContent = t('processing_request');

        try {
            const workspace = document.getElementById('task-workspace').value || null;
            const projectPath = document.getElementById('project-path').value.trim() || null;
            const normalize = value => String(value || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
            const sameWorkspace = this.sessionWorkspace && (projectPath ?
                this.sessionWorkspace.project_path && normalize(projectPath) === normalize(this.sessionWorkspace.project_path) :
                workspace && !this.sessionWorkspace.project_path && workspace === this.sessionWorkspace.workspace);
            const previousSessionId = document.getElementById('include-context').checked && sameWorkspace ? this.sessionId : null;
            // 发送API请求创建新会话
            const response = await fetch('/api/chat', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({ prompt: message, model: document.getElementById('monitor-model')?.value || null,
                    workspace, project_path: projectPath, previous_session_id: previousSessionId }),
            });

            if (!response.ok) {
                const detail = await response.json().catch(() => ({}));
                throw new Error(detail.detail || t('api_error', { status: response.status }));
            }

            const data = await response.json();
            this.sessionId = data.session_id;
            this.sessionWorkspace = {workspace: data.workspace, project_path: data.project_path};
            localStorage.setItem('openmanus-session-id', data.session_id);
            if (data.project_path) this.loadProjectFiles();
            else { document.getElementById('project-browser').hidden = true; this.loadWorkspaceChoices(data.workspace); }

            // 添加用户消息到聊天
            this.chatManager.addUserMessage(message);
            if (data.context_count) this.chatManager.addSystemMessage(`Included ${data.context_count} previous task exchange(s) from this workspace.`);

            // 连接WebSocket
            this.websocketManager.connect(this.sessionId);

            // 重置思考记录
            this.thinkingManager.clearThinking();

        } catch (error) {
            console.error(t('send_message_error', { message: error.message }), error);
            this.chatManager.addSystemMessage(t('error_occurred', { message: error.message }));
            this.isProcessing = false;
            document.getElementById('send-btn').disabled = false;
            document.getElementById('stop-btn').disabled = true;
            document.getElementById('status-indicator').textContent = '';
        }
    }

    // 处理WebSocket消息
    handleWebSocketMessage(data) {
        // 处理状态更新
        if (data.status) {
            if (data.status === 'completed' || data.status === 'error' || data.status === 'stopped') {
                this.isProcessing = false;
                document.getElementById('send-btn').disabled = false;
                document.getElementById('stop-btn').disabled = true;
                document.getElementById('status-indicator').textContent = '';

                // 如果有结果，显示结果
                if (data.result) {
                    this.chatManager.addAIMessage(data.result);
                }
            }
        }

        // 处理思考步骤
        if (data.thinking_steps && data.thinking_steps.length > 0) {
            this.thinkingManager.addThinkingSteps(data.thinking_steps);
        }
        if (data.logs && data.logs.length > 0) {
            this.thinkingManager.addThinkingSteps(data.logs.map(entry => ({
                message: String(entry.message ?? ''), type: 'system_log',
                timestamp: entry.timestamp || Date.now() / 1000
            })));
        }

        // 处理终端输出
        if (data.terminal_output && data.terminal_output.length > 0) {
            console.log('收到终端输出:', data.terminal_output);
        }

        // 处理系统日志 - 将系统日志转换为思考步骤并显示
        if (data.system_logs && data.system_logs.length > 0) {
            console.log('收到系统日志:', data.system_logs);

            // 将系统日志转换为思考步骤格式并添加到思考时间线
            const logSteps = data.system_logs.map(log => {
                return {
                    message: log,
                    type: "system_log",
                    details: null,
                    timestamp: Date.now() / 1000
                };
            });

            this.thinkingManager.addThinkingSteps(logSteps);
        }

        // 处理聊天日志
        if (data.chat_logs && data.chat_logs.length > 0) {
            console.log('收到聊天日志:', data.chat_logs);
        }

        // 如果处理完成，刷新工作区文件
        if (data.status === 'completed') {
            setTimeout(() => this.loadWorkspaceFiles(), 1000);
        }
    }

    // 停止处理
    async stopProcessing() {
        if (!this.sessionId) return;

        try {
            const response = await fetch(`/api/chat/${this.sessionId}/stop`, {
                method: 'POST',
            });

            if (!response.ok) {
                throw new Error(t('api_error', { status: response.status }));
            }

            this.chatManager.addSystemMessage('Stop requested. Canceling model requests and waiting for any running tool.');
            document.getElementById('status-indicator').textContent = 'Stopping…';
            document.getElementById('stop-btn').disabled = true;

        } catch (error) {
            console.error(t('stop_processing_error', { message: error.message }), error);
            this.chatManager.addSystemMessage(t('error_occurred', { message: error.message }));
        }
    }

    // 加载工作区文件
    async loadWorkspaceFiles() {
        try {
            const response = await fetch('/api/files');
            if (!response.ok) {
                throw new Error(t('api_error', { status: response.status }));
            }

            const data = await response.json();
            this.workspaceManager.updateWorkspaces(data.workspaces);
            if (!document.getElementById('project-browser').hidden) this.loadProjectFiles(this.projectDirectory || '');

        } catch (error) {
            console.error(t('load_workspace_error', { message: error.message }), error);
        }
    }

    async loadProjectFiles(path = '') {
        if (!this.sessionId) return;
        try {
            const response = await fetch(`/api/chat/${encodeURIComponent(this.sessionId)}/project-files?path=${encodeURIComponent(path)}`);
            if (!response.ok) throw new Error(`HTTP ${response.status}`);
            const data = await response.json();
            if (!data.entries) { this.fileViewerManager.showFile(data.name, data.content); return; }
            this.projectDirectory = path;
            document.getElementById('project-browser').hidden = false;
            document.getElementById('project-location').textContent = path || '(project root)';
            const list = document.getElementById('project-files');
            list.replaceChildren();
            if (path) {
                const up = document.createElement('button'); up.textContent = '← Parent folder';
                up.onclick = () => this.loadProjectFiles(path.split(/[\\/]/).slice(0, -1).join('/'));
                list.appendChild(up);
            }
            for (const entry of data.entries) {
                const button = document.createElement('button'); button.className = 'file-item';
                button.textContent = (entry.directory ? '📁 ' : '📄 ') + entry.name;
                button.onclick = () => this.loadProjectFiles(entry.path);
                list.appendChild(button);
            }
            if (data.truncated) list.append('Showing first 500 entries.');
        } catch (error) { this.chatManager.addSystemMessage('Could not open project files: ' + error.message); }
    }

    // 处理文件点击
    async handleFileClick(filePath) {
        try {
            const response = await fetch(`/api/files/${encodeURIComponent(filePath)}`);
            if (!response.ok) {
                throw new Error(t('api_error', { status: response.status }));
            }

            const data = await response.json();
            this.fileViewerManager.showFile(data.name, data.content);

        } catch (error) {
            console.error(t('load_file_error', { message: error.message }), error);
            this.chatManager.addSystemMessage(t('error_occurred', { message: error.message }));
        }
    }
}

// 当DOM加载完成后初始化应用
document.addEventListener('DOMContentLoaded', () => {
    const app = new App();
    app.init();

    // 将app实例暴露到全局，方便调试
    window.app = app;
});
