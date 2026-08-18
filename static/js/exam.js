// 申论帮 - 考试页面脚本

let currentPaper = null;
let currentQuestion = null;
let submissionId = null;
const MAX_MODELS_PER_SUBMISSION = 4;
let availableGradingModels = [];

function getExamToken() {
  return localStorage.getItem('slb_token') || localStorage.getItem('token');
}

function getSelectedModelIds() {
  return Array.from(document.querySelectorAll('input[name="model_ids"]:checked'))
    .map(input => input.value);
}

function renderExamModelOptions(models) {
  const panel = document.getElementById('grading-model-panel');
  const list = document.getElementById('model-list');
  if (!panel || !list) return;

  panel.style.display = 'block';
  list.replaceChildren();
  if (!models.length) {
    const empty = document.createElement('div');
    empty.textContent = '暂无公开模型，将继续使用原有批改服务。';
    list.appendChild(empty);
    return;
  }

  models.forEach((model, index) => {
    const label = document.createElement('label');
    const input = document.createElement('input');
    const name = document.createElement('span');
    input.type = 'checkbox';
    input.name = 'model_ids';
    input.value = model.model_id;
    input.checked = index < MAX_MODELS_PER_SUBMISSION;
    input.addEventListener('change', () => {
      const selectedModelIds = getSelectedModelIds();
      if (selectedModelIds.length > 4) {
        input.checked = false;
        showToast('一次最多选择 4 个模型', 'error');
      }
    });
    name.textContent = model.model_name || model.name || '未命名模型';
    label.append(input, name);
    list.appendChild(label);
  });
}

async function loadExamGradingModels() {
  const token = getExamToken();
  if (!token || !document.getElementById('grading-model-panel')) return;
  try {
    const res = await fetch('/api/models', {
      headers: { 'Authorization': 'Bearer ' + token }
    });
    if (!res.ok) throw new Error('model list unavailable');
    const payload = await res.json();
    availableGradingModels = Array.isArray(payload.data?.models) ? payload.data.models : [];
  } catch (error) {
    console.error('Failed to load grading models:', error);
    availableGradingModels = [];
  }
  renderExamModelOptions(availableGradingModels);
}

function getExamGradingSelection() {
  const gradingMode = document.querySelector('input[name="grading-mode"]:checked')?.value || 'fallback';
  const selectedModelIds = getSelectedModelIds();
  if (!availableGradingModels.length) return { gradingMode, selectedModelIds, valid: true };
  if (!selectedModelIds.length) {
    showToast('请至少选择一个批改模型', 'error');
    return { gradingMode, selectedModelIds, valid: false };
  }
  if (selectedModelIds.length > 4) {
    showToast('一次最多选择 4 个模型', 'error');
    return { gradingMode, selectedModelIds, valid: false };
  }
  if (gradingMode === 'ensemble' && selectedModelIds.length < 2) {
    showToast('多模型评审至少选择 2 个模型', 'error');
    return { gradingMode, selectedModelIds, valid: false };
  }
  return { gradingMode, selectedModelIds, valid: true };
}

function escapeExamHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, character => ({
    '&': '&amp;',
    '<': '&lt;',
    '>': '&gt;',
    '"': '&quot;',
    "'": '&#39;'
  })[character]);
}

function formatExamPoint(point) {
  if (typeof point === 'string') return point;
  if (!point || typeof point !== 'object') return '';
  let text = point.point || point.description || '';
  if (point.score !== undefined && point.max_score !== undefined) {
    text += `（${point.score}/${point.max_score}分）`;
  }
  if (point.evidence) text += `；依据：${point.evidence}`;
  if (point.agreement !== undefined && Number.isFinite(Number(point.agreement))) {
    text += `；一致率：${(Number(point.agreement) * 100).toFixed(1)}%`;
  }
  return text;
}

function renderExamPointList(elementId, points) {
  const element = document.getElementById(elementId);
  const safePoints = Array.isArray(points) ? points : [];
  element.innerHTML = safePoints.length
    ? safePoints.map(point => `<li>${escapeExamHtml(formatExamPoint(point))}</li>`).join('')
    : '<li>无</li>';
}

function setExamEmptyState(container, message) {
  const empty = document.createElement('p');
  empty.className = 'empty-state';
  empty.textContent = message;
  container.replaceChildren(empty);
}

// 加载试卷列表
async function loadPapers(page = 1) {
  const container = document.getElementById('papers-container');
  if (!container) return;

  container.innerHTML = '<div class="loading"><div class="spinner"></div></div>';

  try {
    const params = new URLSearchParams({ page, limit: 12 });
    const examType = document.getElementById('filter-exam-type')?.value;
    const year = document.getElementById('filter-year')?.value;
    const search = document.getElementById('filter-search')?.value;

    if (examType) params.append('exam_type', examType);
    if (year) params.append('year', year);
    if (search) params.append('search', search);

    const res = await apiFetch(`/api/papers?${params}`);
    renderPapers(res.data);
  } catch (e) {
    setExamEmptyState(container, `加载失败: ${e.message}`);
  }
}

function renderPapers(data) {
  const container = document.getElementById('papers-container');
  if (!data.papers || data.papers.length === 0) {
    setExamEmptyState(container, '暂无试卷');
    return;
  }

  container.replaceChildren();
  data.papers.forEach(paper => {
    const card = document.createElement('div');
    const title = document.createElement('h4');
    const meta = document.createElement('div');
    const examType = document.createElement('span');
    const year = document.createElement('span');
    const description = document.createElement('p');
    const heat = document.createElement('div');
    const heatIcon = document.createElement('i');
    const heatValue = document.createElement('span');

    card.className = 'card paper-card';
    card.addEventListener('click', () => selectPaper(paper.pid));
    title.textContent = paper.title || '未命名试卷';
    meta.className = 'paper-meta';
    examType.className = 'badge';
    examType.textContent = paper.exam_type || '未分类';
    year.className = 'badge';
    year.textContent = paper.year || '年份未知';
    description.className = 'paper-desc';
    description.textContent = `${Array.isArray(paper.questions) ? paper.questions.length : 0} 道题目`;
    heat.className = 'paper-heat';
    heatIcon.setAttribute('data-lucide', 'activity');
    heatValue.textContent = String(paper.heat || 0);

    meta.append(examType, year);
    heat.append(heatIcon, heatValue);
    card.append(title, meta, description, heat);
    container.appendChild(card);
  });

  lucide.createIcons();
  renderPagination(data);
}

function renderPagination(data) {
  const container = document.getElementById('pagination');
  if (!container || data.pages <= 1) {
    if (container) container.replaceChildren();
    return;
  }

  container.replaceChildren();
  for (let i = 1; i <= data.pages; i++) {
    const button = document.createElement('button');
    button.className = `pagination-btn ${i === data.page ? 'active' : ''}`;
    button.type = 'button';
    button.textContent = String(i);
    button.addEventListener('click', () => loadPapers(i));
    container.appendChild(button);
  }
}

// 选择试卷
async function selectPaper(pid) {
  try {
    const res = await apiFetch(`/api/papers/${pid}`);
    currentPaper = res.data;
    const questions = parseQuestions(currentPaper.questions);

    if (questions.length === 0) {
      showToast('该试卷暂无题目', 'error');
      return;
    }

    showQuestionSelector(questions);
  } catch (e) {
    showToast('加载失败', 'error');
  }
}

function showQuestionSelector(questions) {
  const modal = document.getElementById('question-selector-modal') || createQuestionSelectorModal();
  const list = document.getElementById('question-list');

  list.replaceChildren();
  questions.forEach((question, index) => {
    const item = document.createElement('div');
    const number = document.createElement('div');
    const info = document.createElement('div');
    const stem = document.createElement('p');
    const meta = document.createElement('p');
    const fullStem = String(question.stem || '');

    item.className = 'question-item';
    item.addEventListener('click', () => startExam(index));
    number.className = 'question-number';
    number.textContent = String(index + 1);
    info.className = 'question-info';
    stem.className = 'question-stem';
    stem.textContent = `${fullStem.substring(0, 80)}${fullStem.length > 80 ? '...' : ''}`;
    meta.className = 'question-meta';
    meta.textContent = `字数要求: ${question.word_limit || '未指定'}`;

    info.append(stem, meta);
    item.append(number, info);
    list.appendChild(item);
  });

  modal.classList.add('active');
  lucide.createIcons();
}

function createQuestionSelectorModal() {
  const modal = document.createElement('div');
  modal.id = 'question-selector-modal';
  modal.className = 'modal';
  modal.innerHTML = `
    <div class="modal-content">
      <div class="modal-header">
        <h3>选择题目</h3>
        <button class="modal-close" onclick="closeQuestionSelector()">&times;</button>
      </div>
      <div class="modal-body" id="question-list"></div>
    </div>
  `;
  document.body.appendChild(modal);
  return modal;
}

function closeQuestionSelector() {
  const modal = document.getElementById('question-selector-modal');
  if (modal) modal.classList.remove('active');
}

// 开始考试
async function startExam(questionIndex) {
  closeQuestionSelector();

  const questions = parseQuestions(currentPaper.questions);
  currentQuestion = questions[questionIndex];

  // 显示作答界面
  document.getElementById('exam-section').style.display = 'block';
  document.getElementById('paper-title').textContent = currentPaper.title;

  // 显示材料
  const materialContainer = document.getElementById('material');
  if (currentQuestion.material && currentQuestion.material.length > 0) {
    materialContainer.replaceChildren();
    currentQuestion.material.forEach(material => {
      const paragraph = document.createElement('p');
      paragraph.textContent = material;
      materialContainer.appendChild(paragraph);
    });
  } else {
    setExamEmptyState(materialContainer, '无给定材料');
  }

  // 显示题目
  document.getElementById('question-stem').textContent = currentQuestion.stem;
  document.getElementById('word-limit').textContent = '字数要求: ' + (currentQuestion.word_limit || '未指定');

  // 清空答案
  document.getElementById('answer').value = '';
  document.getElementById('word-count').textContent = '0';

  // 隐藏结果区
  document.getElementById('result-section').style.display = 'none';
  document.getElementById('exam-section').scrollIntoView({ behavior: 'smooth' });

  lucide.createIcons();
}

// 字数统计
document.addEventListener('DOMContentLoaded', function() {
  const answerInput = document.getElementById('answer');
  if (answerInput) {
    answerInput.addEventListener('input', function() {
      document.getElementById('word-count').textContent = this.value.length;
    });
  }
  loadExamGradingModels();
});

// 提交答案
async function submitAnswer() {
  const answer = document.getElementById('answer').value.trim();

  if (!answer) {
    showToast('请输入答案', 'error');
    return;
  }

  if (currentQuestion.word_limit && answer.length > currentQuestion.word_limit * 1.2) {
    if (!confirm(`答案字数(${answer.length})超过字数要求(${currentQuestion.word_limit})的20%，可能影响评分，是否继续提交？`)) {
      return;
    }
  }

  const { gradingMode, selectedModelIds, valid } = getExamGradingSelection();
  if (!valid) return;

  const submitBtn = document.querySelector('.submit-btn');
  submitBtn.disabled = true;
  submitBtn.innerHTML = '<div class="spinner"></div> 提交中...';

  try {
    const res = await apiFetch('/api/submissions', {
      method: 'POST',
      body: JSON.stringify({
        pid: currentPaper.pid,
        qid: currentQuestion.qid,
        user_answer: answer,
        ...(selectedModelIds.length ? {
          mode: gradingMode,
          model_ids: selectedModelIds
        } : {})
      })
    });

    submissionId = res.data.sid;
    showResult(res.data);
  } catch (e) {
    showToast(e.message || '提交失败', 'error');
  } finally {
    submitBtn.disabled = false;
    submitBtn.innerHTML = '<i data-lucide="send"></i> 提交批改';
    lucide.createIcons();
  }
}

// 显示结果
function showResult(data) {
  document.getElementById('exam-section').style.display = 'none';
  document.getElementById('result-section').style.display = 'block';

  // 填充结果数据
  document.getElementById('total-score').textContent = data.score !== null ? Math.round(data.score) : '--';
  document.getElementById('paper-info').textContent = currentPaper.title;

  // 维度得分
  if (data.dimension_scores) {
    const dims = data.dimension_scores;
    const maxScores = { '踩点命中': 40, '逻辑结构': 25, '语言规范': 20, '字数控制': 10, '卷面整洁': 5 };
    const dimensions = document.getElementById('dimension-scores');
    dimensions.replaceChildren();
    Object.entries(dims).forEach(([key, value]) => {
      const item = document.createElement('div');
      const label = document.createElement('span');
      const bar = document.createElement('div');
      const fill = document.createElement('div');
      const score = document.createElement('span');
      const numericValue = Number(value);
      const maximum = Number(maxScores[key] || 100);
      const percentage = Number.isFinite(numericValue)
        ? Math.min(100, Math.max(0, numericValue / maximum * 100))
        : 0;

      item.className = 'dimension-item';
      label.className = 'dimension-label';
      label.textContent = key;
      bar.className = 'dimension-bar';
      fill.className = 'dimension-fill';
      fill.style.width = `${percentage}%`;
      score.className = 'dimension-value';
      score.textContent = `${Number.isFinite(numericValue) ? numericValue : 0}/${maximum}`;

      bar.appendChild(fill);
      item.append(label, bar, score);
      dimensions.appendChild(item);
    });
  }

  // 命中/遗漏要点
  renderExamPointList('hit-points', data.hit_points);
  renderExamPointList('missing-points', data.missing_points);

  // AI反馈
  document.getElementById('ai-feedback').textContent = data.ai_feedback || '暂无反馈';

  // 改进建议
  document.getElementById('improving-suggestions').textContent = data.improving_suggestions || '暂无建议';

  // 用户答案
  document.getElementById('user-answer').textContent = data.user_answer || '';

  document.getElementById('result-section').scrollIntoView({ behavior: 'smooth' });
  lucide.createIcons();
}

// 继续练习
function continuePractice() {
  document.getElementById('result-section').style.display = 'none';
  document.getElementById('exam-section').style.display = 'block';
  document.getElementById('answer').value = '';
  document.getElementById('word-count').textContent = '0';
}

// 解析题目JSON
function parseQuestions(questionsJson) {
  try {
    return JSON.parse(questionsJson);
  } catch {
    return [];
  }
}
