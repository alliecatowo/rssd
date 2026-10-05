<script setup>
import DefaultTheme from 'vitepress/theme'
import { useData } from 'vitepress'
import TreeHome from './TreeHome.vue'

const { frontmatter } = useData()
const Default = DefaultTheme.Layout
</script>

<template>
  <TreeHome v-if="frontmatter.layout === 'tree'" />
  <Default v-else />
</template>
