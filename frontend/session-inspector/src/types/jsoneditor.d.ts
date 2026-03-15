declare module 'jsoneditor' {
  const JSONEditor: unknown
  export default JSONEditor
}

declare module 'json-source-map' {
  export function parse(source: string): {
    pointers: Record<
      string,
      {
        value?: { line: number; column: number; pos: number }
        valueEnd?: { line: number; column: number; pos: number }
      }
    >
  }
}
