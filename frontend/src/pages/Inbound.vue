<template>
  <div>
    <h1>分批入库（先进暂存）</h1>
    <p class="muted">入库先进入暂存，全层与层页都看不到；确认落层后才上架。</p>
    <select v-model.number="item_id"><option v-for="i in items" :key="i.id" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" placeholder="数量" />
    <input v-model="expiry" placeholder="到期 YYYY-MM-DD" />
    <button @click="go">送入暂存</button>
    <p v-if="msg" :class="err ? 'muted' : 'muted'">{{ msg }}</p>

    <h2>暂存区</h2>
    <p class="muted" v-if="!staged.length">暂存已空，没有待落层的批。</p>
    <div v-for="s in staged" :key="s.id" class="lot staged">
      {{ s.name }} ×{{ s.qty }}{{ s.unit }} · 到期 {{ s.expiry }} · {{ layerLabel[s.layer] }}
      <button class="confirm" @click="confirm(s)">确认落层</button>
    </div>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const items = ref([])
const staged = ref([])
const item_id = ref(1)
const qty = ref(1)
const expiry = ref('2026-12-01')
const msg = ref('')
const err = ref(false)
const layerLabel = { upper: '上层', mid: '中层', lower: '下层' }
async function loadItems() {
  items.value = await api('/items')
  if (items.value[0]) item_id.value = items.value[0].id
}
async function loadStaged() { staged.value = await api('/staged') }
function flash(text, isErr = false) { msg.value = text; err.value = isErr }
async function go() {
  try {
    await api('/lots', { method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value, expiry: expiry.value }) })
    flash('已进入暂存，等待确认落层')
    await loadStaged()
  } catch (e) {
    flash('入库失败：' + e.message, true)
  }
}
async function confirm(s) {
  try {
    const r = await api('/staged/confirm', { method: 'POST', body: JSON.stringify({ staged_id: s.id }) })
    flash(`已落层：${s.name} 上架到${layerLabel[r.layer]}，全层与该层页可见`)
    await loadStaged()
  } catch (e) {
    flash('确认失败（数量非正或品项不存在）：' + e.message, true)
  }
}
onMounted(async () => { await loadItems(); await loadStaged() })
</script>
