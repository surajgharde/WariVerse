import { ScrollViewStyleReset } from 'expo-router/html';
import React from 'react';

export default function RootLayoutHTML({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <head>
        <meta charSet="utf-8" />
        <meta httpEquiv="X-UA-Compatible" content="IE=edge" />
        <meta name="viewport" content="width=device-width, initial-scale=1, shrink-to-fit=no" />
        <ScrollViewStyleReset />
        <style
          dangerouslySetInnerHTML={{
            __html: `
              textarea, input, button, select {
                outline: none !important;
                box-shadow: none !important;
                -webkit-tap-highlight-color: transparent;
              }
              textarea:focus, input:focus, button:focus {
                outline: none !important;
                box-shadow: none !important;
              }
            `,
          }}
        />
      </head>
      <body>{children}</body>
    </html>
  );
}
