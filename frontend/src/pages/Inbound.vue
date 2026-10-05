<template>
  <div>
    <h1>分批入库</h1>
    <p class="muted">入库先落暂存区,确认落层后才上架出现在全层与对应层页。</p>
    <select v-model.number="item_id"><option v-for="i in items" :value="i.id">{{ i.name }}</option></select>
    <input type="number" v-model.number="qty" placeholder="数量" />
    <input v-model="expiry" placeholder="到期 YYYY-MM-DD" />
    <button @click="go">入库</button>
    <p class="muted" v-if="msg">{{ msg }}</p>

    <h2>暂存区</h2>
    <p class="muted" v-if="!staging.length">暂存为空:新入库的批会先停在这里。</p>
    <div v-for="s in staging" :key="s.id" class="lot">
      <span>
        {{ s.name || ('品项#' + s.item_id) }} ×{{ s.qty }} · {{ s.expiry }}
        <em v-if="expired(s)" class="tag-expired">已过期</em>
      </span>
      <button @click="confirm(s)">确认落层</button>
    </div>
    <p class="muted" v-if="err">{{ err }}</p>
  </div>
</template>
<script setup>
import { ref, onMounted } from 'vue'
import { api } from '../api'
const items = ref([])
const staging = ref([])
const item_id = ref(1)
const qty = ref(1)
const expiry = ref('2026-12-01')
const msg = ref('')
const err = ref('')
const today = new Date().toISOString().slice(0, 10)
function expired(s) { return s.expiry && s.expiry < today }
async function load() {
  items.value = await api('/items')
  if (items.value[0]) item_id.value = items.value[0].id
  staging.value = await api('/staging')
}
async function go() {
  err.value = ''
  try {
    await api('/lots', { method: 'POST', body: JSON.stringify({ item_id: item_id.value, qty: qty.value, expiry: expiry.value }) })
    msg.value = '已入暂存,待确认落层'
  } catch (e) { err.value = e.message }
  staging.value = await api('/staging')
}
async function confirm(s) {
  err.value = ''
  try {
    await api('/staging/' + s.id + '/confirm', { method: 'POST', body: '{}' })
    msg.value = '已落层上架:全层与对应层页可见'
  } catch (e) { err.value = '落层失败:' + e.message }
  staging.value = await api('/staging')
}
onMounted(load)
</script>
