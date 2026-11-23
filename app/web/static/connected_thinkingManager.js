// connected_thinkingManager.js - 处理AI思考过程显示

export class ThinkingManager {
    constructor() {
        this.thinkingContainer = document.getElementById('thinking-timeline');
        this.recordCountElement = document.getElementById('record-count');
        this.autoScrollCheckbox = document.getElementById('auto-scroll');
        this.thinkingSteps = [];
    }

    // 初始化思考管理器
    init() {
        // 初始化记录计数
        this.updateRecordCount();
    }

    // 添加思考步骤
    addThinkingStep(step) {
        this.thinkingSteps.push(step);

        // 创建并添加步骤元素
        const stepElement = this.createStepElement(step);
        this.thinkingContainer.appendChild(stepElement);

        // 更新记录计数
        this.updateRecordCount();

        // 如果启用了自动滚动，滚动到底部
        if (this.autoScrollCheckbox.checked) {
            this.scrollToBottom();
        }

        // 淡入效果
        setTimeout(() => {
            stepElement.style.opacity = 1;
        }, 10);
    }

    // 添加多个思考步骤
    addThinkingSteps(steps) {
        if (!Array.isArray(steps)) return;

        steps.forEach(step => {
            this.addThinkingStep(step);
        });
    }

    // 创建步骤元素
    createStepElement(step) {
        const itemDiv = document.createElement('div');
        itemDiv.className = 'timeline-item';
        itemDiv.style.opacity = 0; // 初始透明，用于淡入效果

        // 如果是完成步骤，添加completed类
        if (step.type === 'conclusion' || step.type === 'completed') {
            itemDiv.classList.add('completed');
        }

        // 创建标记点
        const markerDiv = document.createElement('div');
        markerDiv.className = 'timeline-marker';
        itemDiv.appendChild(markerDiv);

        // 创建内容容器
        const contentDiv = document.createElement('div');
        contentDiv.className = 'timeline-content';

        // 创建标题
        const headerDiv = document.createElement('div');
        headerDiv.className = 'timeline-header';
        headerDiv.textContent = this.getStepHeader(step);
        contentDiv.appendChild(headerDiv);

        // 如果是通信类型的步骤
        if (step.type === 'communication') {
            const headerDiv = document.createElement('div');
            headerDiv.className = 'communication-header';
            const direction = document.createElement('span');
            direction.className = 'communication-direction';
            direction.textContent = step.message;
            const icon = document.createElement('span');
            icon.className = 'toggle-icon';
            icon.textContent = '▶';
            headerDiv.append(direction, ' ', icon);
            headerDiv.onclick = function() {
                const detailsElement = this.nextElementSibling;
                const toggleIcon = this.querySelector('.toggle-icon');

                if (detailsElement.style.display === 'none' || !detailsElement.style.display) {
                    detailsElement.style.display = 'block';
                    toggleIcon.textContent = '▼';
                } else {
                    detailsElement.style.display = 'none';
                    toggleIcon.textContent = '▶';
                }
            };

            const detailsElement = document.createElement('div');
            detailsElement.className = 'timeline-details';
            detailsElement.style.display = 'none';

            if (step.details) {
                detailsElement.textContent = step.details;
            } else {
                detailsElement.textContent = '(No details)';
            }

            contentDiv.appendChild(headerDiv);
            contentDiv.appendChild(detailsElement);
        }
        // 如果有详细内容，添加详情按钮和内容
        else if (step.details) {
            // 创建详情按钮
            const detailsButton = document.createElement('button');
            detailsButton.className = 'btn-details';
            detailsButton.textContent = 'Show details ▼';
            contentDiv.appendChild(detailsButton);

            // 创建详情内容（初始隐藏）
            const detailsDiv = document.createElement('div');
            detailsDiv.className = 'timeline-details';
            detailsDiv.style.display = 'none';
            detailsDiv.textContent = step.details;
            contentDiv.appendChild(detailsDiv);

            // 绑定详情按钮点击事件
            detailsButton.addEventListener('click', () => {
                if (detailsDiv.style.display === 'none') {
                    detailsDiv.style.display = 'block';
                    detailsButton.textContent = 'Hide details ▲';
                } else {
                    detailsDiv.style.display = 'none';
                    detailsButton.textContent = 'Show details ▼';
                }
            });
        }

        // 如果是文件生成步骤，添加文件列表
        if (step.files && step.files.length > 0) {
            const fileListDiv = document.createElement('div');
            fileListDiv.className = 'file-list';
            fileListDiv.textContent = step.files.join(', ');
            contentDiv.appendChild(fileListDiv);
        }

        itemDiv.appendChild(contentDiv);
        return itemDiv;
    }

    // 获取步骤标题
    getStepHeader(step) {
        if (step.message) {
            return step.message;
        }

        switch (step.type) {
            case 'thinking':
                return step.content || 'Thinking process';
            case 'tool':
                return `Using tool: ${step.tool || ''}`;
            case 'file':
                return `Generated ${step.files ? step.files.length : 0} files in workspace ${step.workspace || ''}:`;
            case 'conclusion':
            case 'completed':
                return `Task completed! Results saved to workspace ${step.workspace || ''}.`;
            case 'error':
                return `Error: ${step.error || ''}`;
            case 'system':
                return step.content || 'System message';
            case 'system_log':
                return step.message || 'System log';
            case 'progress':
                return `Executing step ${step.current}/${step.total}`;
            case 'communication':
                return step.message || 'Communication';
            default:
                return step.content ? step.content.substring(0, 50) + (step.content.length > 50 ? '...' : '') : 'Thinking step';
        }
    }

    // 更新记录计数
    updateRecordCount() {
        if (this.recordCountElement) {
            this.recordCountElement.textContent = `${this.thinkingSteps.length} records`;
        }
    }

    // 清除所有思考记录
    clearThinking() {
        this.thinkingSteps = [];
        this.thinkingContainer.innerHTML = '';
        this.updateRecordCount();
    }

    // 滚动到底部
    scrollToBottom() {
        this.thinkingContainer.scrollTop = this.thinkingContainer.scrollHeight;
    }
}
